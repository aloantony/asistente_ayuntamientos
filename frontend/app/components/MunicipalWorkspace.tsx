"use client";

import {
  BookOpen,
  Building2,
  CircleAlert,
  ClipboardList,
  Landmark,
  Map as MapIcon,
  RefreshCw,
  ShieldCheck,
  Users,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  Suspense,
  useEffect,
  useRef,
  useState,
  type DragEvent,
  type KeyboardEvent,
} from "react";
import { fetchMunicipality } from "../lib/fetchers";
import { fetchMaintenanceOrders } from "../lib/maintenance";
import {
  fetchGovernmentMembers,
  fetchMunicipalAssetSummary,
  fetchMunicipalMaintenanceSummary,
  fetchMunicipalOrdinances,
  fetchMunicipalOrganization,
  fetchStaffPosts,
  fetchStaffWorkers,
  type MunicipalCollection,
} from "../lib/municipalWorkspace";
<<<<<<< HEAD
import {
  canViewGovernment as hasGovernmentAccess,
  canViewMunicipalHub,
  canViewStaff as hasStaffAccess,
} from "../lib/permissions";
import { useSession } from "../lib/session";
=======
import { townHallShieldUrl } from "../lib/api";
import { canViewMunicipalHub } from "../lib/permissions";
import {
  canEditTownHall,
  shouldShowProjectsPanel,
  shouldShowRequirementsPanel,
  useSession,
} from "../lib/session";
import { useTownHallController } from "../lib/useTownHallController";
import { TownHallContentPanel } from "./TownHallContentPanel";
import { TownHallEpigraphCard } from "./TownHallEpigraphCard";
import { TownHallNavEditor } from "./TownHallNavEditor";
>>>>>>> origin/servidor-main-backup
import styles from "./MunicipalWorkspace.module.css";
import {
  fetchClimateSeries,
  fetchHouseholdSeries,
  fetchPadronSeries,
} from "../lib/municipalData";
import {
  fetchContracts,
  fetchGrants,
  fetchLicences,
  fetchNotices,
  fetchOfficeHours,
} from "../lib/administration";
import {
  fetchBudgetExecution,
  fetchBudgets,
  fetchCouncilSessions,
  fetchTreasuryMovements,
} from "../lib/budgets";
import { Administracion } from "./ayuntamiento/Administracion";
import { fetchArchiveItems, fetchHeritageAssets } from "../lib/heritage";
import { MapaGeneral } from "./ayuntamiento/MapaGeneral";
import { Patrimonio } from "./ayuntamiento/Patrimonio";
import { Plenos } from "./ayuntamiento/Plenos";
import { Presupuestos } from "./ayuntamiento/Presupuestos";
import { Comunicacion } from "./ayuntamiento/Comunicacion";
import { EstructuraGobierno } from "./ayuntamiento/EstructuraGobierno";
import { SeriesMunicipio } from "./ayuntamiento/SeriesMunicipio";
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
  type ArchiveItem,
  type BudgetExecution,
  type HeritageAsset,
  type ClimateRecord,
  type CouncilSession,
  type MunicipalBudget,
  type TreasuryMovement,
  type MunicipalContract,
  type MunicipalGrant,
  type MunicipalLicence,
  type MunicipalNotice,
  type OfficeHour,
  type GovernmentMember,
  type HouseholdStat,
  type PadronRecord,
  type MaintenanceOrder,
  type Municipality,
  type MunicipalAsset,
  type Organization,
  type Ordinance,
<<<<<<< HEAD
  type StaffPost,
  type StaffWorker,
  type User,
} from "./types";

=======
  type TownHall,
  type TownHallNavSection,
  type User,
} from "./types";

type WorkspaceTab =
  | "summary"
  | "ordinances"
  | "facilities"
  | "people"
  | "roadmap";

// Las áreas fijas conservan sus claves literales; los apartados que el usuario
// crea en el editor del menú se identifican con el id de su bloque (ADR-034).
type CustomTab = `block-${number}`;
type ActiveTab = WorkspaceTab | CustomTab;

function isCustomTab(tab: string): tab is CustomTab {
  return /^block-\d+$/.test(tab);
}

function customTabBlockId(tab: CustomTab) {
  return Number(tab.slice("block-".length));
}

type MunicipalContext = {
  organization: OrganizationSummary;
  municipality: MunicipalitySummary;
};

type ResourceErrors = {
  ordinances: string;
  assets: string;
  maintenance: string;
};

>>>>>>> origin/servidor-main-backup
type TabDefinition = {
  id: ActiveTab;
  label: string;
  icon: LucideIcon;
};

<<<<<<< HEAD
// Rótulos tomados de la navegación municipal de referencia. Los identificadores
// no cambian: se usan en enlaces `?tab=` repartidos por el producto.
=======
/** Localiza la pestaña de un bloque: si es un epígrafe, la de su pestaña padre.
 *  Sirve para que los enlaces antiguos a un epígrafe sigan llevando a su sitio,
 *  ahora que un epígrafe es una tarjeta dentro de una pestaña y no una pestaña. */
function findTabForBlock(townHall: TownHall | null, tab: ActiveTab) {
  if (townHall === null || !isCustomTab(tab)) {
    return null;
  }

  const blockId = customTabBlockId(tab);
  for (const section of townHall.nav) {
    if (section.id === blockId) {
      return { sectionId: section.id, epigraphId: null as number | null };
    }
    if (section.items.some((candidate) => candidate.id === blockId)) {
      return { sectionId: section.id, epigraphId: blockId };
    }
  }

  return null;
}

/** Reordena los epígrafes de una pestaña colocando el arrastrado ante el
 *  destino. Devuelve el árbol completo porque es lo que guarda el backend. */
function moveEpigraph(
  nav: TownHallNavSection[],
  sectionId: number,
  draggedId: number,
  targetId: number,
) {
  if (draggedId === targetId) {
    return null;
  }

  const section = nav.find((candidate) => candidate.id === sectionId);
  const from = section?.items.findIndex((item) => item.id === draggedId) ?? -1;
  const to = section?.items.findIndex((item) => item.id === targetId) ?? -1;

  if (section === undefined || from === -1 || to === -1) {
    return null;
  }

  const items = [...section.items];
  const [moved] = items.splice(from, 1);
  items.splice(to, 0, moved);

  return nav.map((candidate) =>
    candidate.id === sectionId ? { ...candidate, items } : candidate,
  );
}

>>>>>>> origin/servidor-main-backup
const TAB_DEFINITIONS: TabDefinition[] = [
  { id: "summary", label: "Información", icon: Landmark },
  { id: "ordinances", label: "Normativa", icon: BookOpen },
  { id: "facilities", label: "Servicios municipales", icon: Wrench },
  { id: "map", label: "Mapa general", icon: MapIcon },
  { id: "people", label: "Personal", icon: Users },
  { id: "roadmap", label: "Hoja de ruta", icon: ClipboardList },
];

function isWorkspaceTab(value: string | null): value is WorkspaceTab {
  return TAB_DEFINITIONS.some(({ id }) => id === value);
}

// La pestaña vive en la URL, no en estado local: así un enlace `?tab=` abre
// donde dice, el botón atrás funciona y el rótulo no se queda desincronizado.
export function resolveWorkspaceTab(
  searchParams: Pick<URLSearchParams, "get">,
): WorkspaceTab {
  const requestedTab = searchParams.get("tab");
  return isWorkspaceTab(requestedTab) ? requestedTab : "summary";
}

<<<<<<< HEAD
export function buildWorkspaceTabHref(
  pathname: string,
  searchParams: Pick<URLSearchParams, "toString">,
  tab: WorkspaceTab,
=======
function formatDecimal(value: number | null, suffix: string) {
  if (value === null) {
    return "No consta";
  }
  return `${new Intl.NumberFormat("es-ES", {
    maximumFractionDigits: 2,
  }).format(value)} ${suffix}`;
}

function formatDate(value: string | null) {
  if (!value) {
    return "Sin fecha";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "Sin fecha";
  }
  return new Intl.DateTimeFormat("es-ES", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    timeZone: "Europe/Madrid",
  }).format(date);
}

function formatUpdatedAt(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "Fecha no disponible";
  }
  return new Intl.DateTimeFormat("es-ES", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Europe/Madrid",
  }).format(date);
}

/** Agrupa por materia, que es la «categoría» del prototipo, en orden alfabético
 *  y conservando dentro el orden que trae el repositorio. */
function groupOrdinancesByTopic(ordinances: Ordinance[]) {
  const groups = new Map<string, Ordinance[]>();

  for (const ordinance of ordinances) {
    const topic = ordinance.topic?.trim() || "Sin materia";
    const group = groups.get(topic);
    if (group) {
      group.push(ordinance);
    } else {
      groups.set(topic, [ordinance]);
    }
  }

  return [...groups.entries()].sort(([left], [right]) =>
    left.localeCompare(right, "es"),
  );
}

type DueFilter = "all" | "soon" | "overdue";

const DUE_FILTERS: { id: DueFilter; label: string }[] = [
  { id: "all", label: "Todas" },
  { id: "soon", label: "Vence pronto (≤30 días)" },
  { id: "overdue", label: "Solo vencidas" },
];

/** Una orden abierta cuya fecha prevista ya pasó. */
function isOverdue(order: MaintenanceOrder) {
  if (!order.scheduled_for) {
    return false;
  }
  if (order.status === "completed" || order.status === "cancelled") {
    return false;
  }
  return order.scheduled_for < new Date().toISOString().slice(0, 10);
}

/** «1950: 812» por línea. Se ignora lo que no cuadre en vez de fallar: quien
 *  escribe una serie a mano deja líneas a medias, y perder las buenas por una
 *  mala sería peor que descartar esa. */
function parseSeriesPoints(raw: string) {
  const points: { x: string; y: number }[] = [];

  for (const line of raw.split("\n")) {
    const separator = line.lastIndexOf(":");
    if (separator === -1) {
      continue;
    }
    const x = line.slice(0, separator).trim();
    const y = Number(line.slice(separator + 1).trim().replace(",", "."));
    if (x && Number.isFinite(y)) {
      points.push({ x, y });
    }
  }

  return points;
}

function getInitials(value: string) {
  const initials = value
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("");
  return initials || "AY";
}

function withOrganization(path: string, organizationId: number) {
  const separator = path.includes("?") ? "&" : "?";
  return `${path}${separator}organization_id=${organizationId}`;
}

function permissionValue<T>(
  canView: boolean,
  data: MunicipalCollection<T> | null,
  error: string,
>>>>>>> origin/servidor-main-backup
) {
  const params = new URLSearchParams(searchParams.toString());
  // «summary» es el valor por defecto: no se escribe en la URL para que la
  // dirección de la pantalla inicial quede limpia.
  if (tab === "summary") {
    params.delete("tab");
  } else {
    params.set("tab", tab);
  }
<<<<<<< HEAD
  const query = params.toString();
  return query ? `${pathname}?${query}` : pathname;
=======
  if (error) {
    return "No disponible";
  }
  return data?.total ?? "—";
}

function Metric({
  icon: Icon,
  label,
  value,
  detail,
}: {
  icon: LucideIcon;
  label: string;
  value: number | string;
  detail?: string;
}) {
  return (
    <div className={styles.metric}>
      <div className={styles.metricHeading}>
        <span>{label}</span>
        <Icon aria-hidden="true" size={18} strokeWidth={1.6} />
      </div>
      <strong className={typeof value === "number" ? undefined : styles.metricText}>
        {value}
      </strong>
      {detail ? <small>{detail}</small> : null}
    </div>
  );
}

function SectionHeading({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow: string;
  title: string;
  description: string;
  actions?: ReactNode;
}) {
  return (
    <header className={styles.sectionHeading}>
      <div>
        <p>{eyebrow}</p>
        <h2>{title}</h2>
        <span>{description}</span>
      </div>
      {actions ? <div className={styles.sectionActions}>{actions}</div> : null}
    </header>
  );
}

function NotConfigured({
  icon: Icon,
  title,
  description,
  wide = false,
}: {
  icon: LucideIcon;
  title: string;
  description: string;
  wide?: boolean;
}) {
  return (
    <article
      className={`${styles.notConfigured}${wide ? ` ${styles.notConfiguredWide}` : ""}`}
    >
      <span className={styles.notConfiguredIcon}>
        <Icon aria-hidden="true" size={20} strokeWidth={1.6} />
      </span>
      <div>
        <p>No configurado</p>
        <h3>{title}</h3>
        <span>{description}</span>
      </div>
    </article>
  );
}

function ResourceState({
  icon: Icon,
  title,
  description,
  tone = "empty",
  action,
}: {
  icon: LucideIcon;
  title: string;
  description: string;
  tone?: "empty" | "error" | "restricted";
  action?: ReactNode;
}) {
  return (
    <div
      className={styles.resourceState}
      data-tone={tone}
      role={tone === "error" ? "alert" : undefined}
    >
      <Icon aria-hidden="true" size={24} strokeWidth={1.6} />
      <div>
        <h3>{title}</h3>
        <p>{description}</p>
        {action ? <div className={styles.resourceStateAction}>{action}</div> : null}
      </div>
    </div>
  );
}

function SummaryTab({
  municipality,
  organization,
  ordinances,
  assets,
  maintenance,
  errors,
  canViewOrdinances,
  canViewAssets,
  canViewMaintenance,
  onTabChange,
}: {
  municipality: Municipality;
  organization: Organization;
  ordinances: MunicipalCollection<Ordinance> | null;
  assets: MunicipalCollection<MunicipalAsset> | null;
  maintenance: MunicipalCollection<MaintenanceOrder> | null;
  errors: ResourceErrors;
  canViewOrdinances: boolean;
  canViewAssets: boolean;
  canViewMaintenance: boolean;
  onTabChange: (tab: WorkspaceTab) => void;
}) {
  return (
    <div className={styles.tabContent}>
      <div className={styles.metricsGrid}>
        <Metric
          detail="Padrón registrado en la ficha"
          icon={Users}
          label="Población"
          value={formatInteger(municipality.population)}
        />
        <Metric
          detail={formatDecimal(municipality.surface_km2, "km²")}
          icon={MapPin}
          label="Densidad"
          value={formatDecimal(municipality.density, "hab./km²")}
        />
        <Metric
          detail="Repositorio municipal"
          icon={BookOpen}
          label="Normas"
          value={permissionValue(canViewOrdinances, ordinances, errors.ordinances)}
        />
        <Metric
          detail="Inventario no archivado"
          icon={Database}
          label="Activos"
          value={permissionValue(canViewAssets, assets, errors.assets)}
        />
        <Metric
          detail="Órdenes abiertas"
          icon={Hammer}
          label="Mantenimiento"
          value={permissionValue(
            canViewMaintenance,
            maintenance,
            errors.maintenance,
          )}
        />
        <Metric
          detail="Directorio de la organización"
          icon={UserRound}
          label="Personas"
          value={organization.users.length}
        />
      </div>

      <div className={styles.summaryGrid}>
        <article className={styles.card}>
          <div className={styles.cardHeading}>
            <div>
              <p>Ficha oficial</p>
              <h2>Identidad municipal</h2>
            </div>
            <span className={styles.statusPill} data-tone={municipality.status}>
              {municipality.status === "active" ? "Activa" : "Archivada"}
            </span>
          </div>
          <dl className={styles.definitionGrid}>
            <div>
              <dt>Código INE</dt>
              <dd>{municipality.ine_code ?? "No consta"}</dd>
            </div>
            <div>
              <dt>Código postal</dt>
              <dd>{municipality.postal_codes ?? "No consta"}</dd>
            </div>
            <div>
              <dt>Tipo</dt>
              <dd>{MUNICIPALITY_TYPE_LABELS[municipality.municipality_type]}</dd>
            </div>
            <div>
              <dt>Perfil territorial</dt>
              <dd>
                {RURAL_URBAN_PROFILE_LABELS[municipality.rural_urban_profile]}
              </dd>
            </div>
            <div>
              <dt>Provincia</dt>
              <dd>{municipality.province}</dd>
            </div>
            <div>
              <dt>Comunidad autónoma</dt>
              <dd>{municipality.autonomous_community}</dd>
            </div>
          </dl>
          <p className={styles.updatedAt}>
            Actualizada el {formatUpdatedAt(municipality.updated_at)}
          </p>
        </article>

        <article className={styles.card}>
          <div className={styles.cardHeading}>
            <div>
              <p>Contexto</p>
              <h2>Perfiles municipales</h2>
            </div>
          </div>
          <dl className={styles.profileList}>
            <div>
              <dt>Actividad económica</dt>
              <dd>{municipality.economic_profile ?? "No consta información."}</dd>
            </div>
            <div>
              <dt>Turismo</dt>
              <dd>{municipality.tourism_profile ?? "No consta información."}</dd>
            </div>
            <div>
              <dt>Geografía</dt>
              <dd>{municipality.geographic_notes ?? "No consta información."}</dd>
            </div>
            <div>
              <dt>Administración</dt>
              <dd>
                {municipality.administrative_notes ?? "No consta información."}
              </dd>
            </div>
          </dl>
        </article>
      </div>

      <section className={styles.card}>
        <SectionHeading
          description="Accesos directos a la información que ya está conectada al backend."
          eyebrow="Operativa"
          title="Áreas municipales"
        />
        <div className={styles.areaGrid}>
          <button type="button" onClick={() => onTabChange("ordinances")}>
            <BookOpen aria-hidden="true" size={20} strokeWidth={1.6} />
            <span>
              <strong>Normativa</strong>
              <small>
                {canViewOrdinances && ordinances
                  ? `${ordinances.total} documentos registrados`
                  : "Consulta condicionada por permisos"}
              </small>
            </span>
          </button>
          <button type="button" onClick={() => onTabChange("facilities")}>
            <Wrench aria-hidden="true" size={20} strokeWidth={1.6} />
            <span>
              <strong>Instalaciones</strong>
              <small>Inventario, mapa y mantenimiento</small>
            </span>
          </button>
          <button type="button" onClick={() => onTabChange("people")}>
            <Users aria-hidden="true" size={20} strokeWidth={1.6} />
            <span>
              <strong>Personal</strong>
              <small>{organization.users.length} personas en el directorio</small>
            </span>
          </button>
          <button type="button" onClick={() => onTabChange("roadmap")}>
            <ClipboardList aria-hidden="true" size={20} strokeWidth={1.6} />
            <span>
              <strong>Hoja de ruta</strong>
              <small>Planificación municipal y trabajo pendiente</small>
            </span>
          </button>
        </div>
      </section>

      <section>
        <SectionHeading
          description="El diseño contempla estas áreas, pero todavía no existe un modelo persistente que permita mostrarlas con garantías."
          eyebrow="Siguiente capa de datos"
          title="Pendiente de configuración"
        />
        <div className={styles.notConfiguredGrid}>
          <NotConfigured
            description="No hay un registro validado de alcaldía, concejalías u órganos colegiados."
            icon={Building2}
            title="Gobierno y corporación"
          />
          <NotConfigured
            description="No existe una serie meteorológica municipal conectada y trazable."
            icon={CloudSun}
            title="Clima"
          />
          <NotConfigured
            description="Analíticas, depósitos y red de abastecimiento requieren un módulo propio."
            icon={Droplets}
            title="Agua"
          />
        </div>
      </section>
    </div>
  );
}

function OrdinancesTab({
  municipality,
  ordinances,
  error,
  canView,
  canManage,
  onRetry,
}: {
  municipality: Municipality;
  ordinances: MunicipalCollection<Ordinance> | null;
  error: string;
  canView: boolean;
  canManage: boolean;
  onRetry: () => void;
}) {
  const adminHref = `/admin/ordenanzas?q=${encodeURIComponent(municipality.name)}`;

  return (
    <div className={styles.tabContent}>
      <SectionHeading
        actions={
          canManage ? (
            <Link className={styles.primaryAction} href={adminHref}>
              Gestionar repositorio
              <ExternalLink aria-hidden="true" size={15} />
            </Link>
          ) : undefined
        }
        description="Ordenanzas y reglamentos vinculados a la ficha municipal, con su fuente y estado de vigencia."
        eyebrow="Repositorio municipal"
        title="Normativa"
      />

      {!canView ? (
        <ResourceState
          description="Tu cuenta no dispone del permiso ordinances.view para consultar el repositorio."
          icon={ShieldCheck}
          title="Consulta no autorizada"
          tone="restricted"
        />
      ) : error ? (
        <ResourceState
          action={
            <button className={styles.secondaryAction} onClick={onRetry} type="button">
              Reintentar
            </button>
          }
          description={error}
          icon={CircleAlert}
          title="No se pudo cargar la normativa"
          tone="error"
        />
      ) : ordinances && ordinances.items.length > 0 ? (
        <section className={styles.collectionCard}>
          <div className={styles.collectionSummary}>
            <div>
              <strong>{ordinances.total}</strong>
              <span>documentos no archivados</span>
            </div>
            <p>
              Se muestran {ordinances.items.length} de {ordinances.total}.
            </p>
          </div>
          {groupOrdinancesByTopic(ordinances.items).map(([topic, group]) => (
          <div className={styles.ordinanceGroup} key={topic}>
            <h3 className={styles.ordinanceGroupHeading}>
              <span>{topic}</span>
              <small>
                {group.length} {group.length === 1 ? "documento" : "documentos"}
              </small>
            </h3>
          <div className={styles.ordinanceList}>
            {group.map((ordinance) => (
              <article key={ordinance.id}>
                <div className={styles.ordinanceIcon}>
                  <FileText aria-hidden="true" size={20} strokeWidth={1.6} />
                </div>
                <div className={styles.ordinanceMain}>
                  <div className={styles.itemHeading}>
                    <div>
                      <span>{formatOrdinanceType(ordinance.ordinance_type)}</span>
                      <h3>{ordinance.title}</h3>
                    </div>
                    <span
                      className={styles.statusPill}
                      data-tone={ordinance.status}
                    >
                      {formatOrdinanceStatus(ordinance.status)}
                    </span>
                  </div>
                  <p>
                    {ordinance.summary ??
                      "El repositorio no incluye todavía un resumen de este documento."}
                  </p>
                  <div className={styles.itemMeta}>
                    {ordinance.subtopic ? <span>{ordinance.subtopic}</span> : null}
                    <span>
                      Publicación: {formatDate(ordinance.publication_date)}
                    </span>
                    {ordinance.official_bulletin ? (
                      <span>{ordinance.official_bulletin}</span>
                    ) : null}
                  </div>
                  {ordinance.source_url ? (
                    <a
                      href={ordinance.source_url}
                      rel="noreferrer noopener"
                      target="_blank"
                    >
                      Consultar fuente oficial
                      <ExternalLink aria-hidden="true" size={14} />
                    </a>
                  ) : (
                    <span className={styles.missingSource}>
                      Fuente oficial sin enlace registrado
                    </span>
                  )}
                </div>
              </article>
            ))}
          </div>
          </div>
          ))}
        </section>
      ) : (
        <ResourceState
          action={
            canManage ? (
              <Link className={styles.secondaryAction} href={adminHref}>
                Añadir normativa
              </Link>
            ) : undefined
          }
          description="El municipio no tiene ordenanzas activas registradas en el repositorio."
          icon={FileStack}
          title="Repositorio vacío"
        />
      )}

      <NotConfigured
        description="Noticias, bandos, actas, páginas informativas y documentos generales necesitan un CMS municipal con revisión y publicación. No se sustituyen por contenido de demostración."
        icon={FileStack}
        title="Biblioteca y CMS municipal"
        wide
      />
    </div>
  );
}

function FacilitiesTab({
  assets,
  maintenance,
  errors,
  canViewAssets,
  canViewMaintenance,
  canViewMap,
  organizationId,
  onRetry,
}: {
  assets: MunicipalCollection<MunicipalAsset> | null;
  maintenance: MunicipalCollection<MaintenanceOrder> | null;
  errors: ResourceErrors;
  canViewAssets: boolean;
  canViewMaintenance: boolean;
  canViewMap: boolean;
  organizationId: number;
  onRetry: () => void;
}) {
  const inventoryHref = withOrganization("/inventario", organizationId);
  const maintenanceHref = withOrganization("/mantenimiento", organizationId);
  const mapHref = withOrganization("/mapa", organizationId);

  // Filtro de vencimiento del prototipo. Se resuelve en el servidor, no sobre
  // la página ya cargada: si no, "solo vencidas" mentiría en cuanto hubiera
  // más órdenes de las que caben en la primera página.
  const [dueFilter, setDueFilter] = useState<DueFilter>("all");
  const [dueOrders, setDueOrders] = useState<MaintenanceOrder[] | null>(null);
  const [dueError, setDueError] = useState("");

  useEffect(() => {
    if (!canViewMaintenance || dueFilter === "all") {
      setDueOrders(null);
      setDueError("");
      return;
    }

    const controller = new AbortController();
    const today = new Date();
    const bound = new Date(today);
    if (dueFilter === "soon") {
      bound.setDate(bound.getDate() + 30);
    }

    fetchMaintenanceOrders(
      {
        organizationId,
        scheduledTo: bound.toISOString().slice(0, 10),
        includeClosed: false,
        limit: 50,
      },
      controller.signal,
    )
      .then((page) => setDueOrders(page.items))
      .catch(() => {
        if (!controller.signal.aborted) {
          setDueOrders([]);
          setDueError("No se pudo filtrar el mantenimiento por vencimiento.");
        }
      });

    return () => controller.abort();
  }, [canViewMaintenance, dueFilter, organizationId]);

  const shownOrders =
    dueFilter === "all" ? (maintenance?.items ?? []) : (dueOrders ?? []);

  return (
    <div className={styles.tabContent}>
      <SectionHeading
        actions={
          <>
            {canViewMap ? (
              <Link className={styles.secondaryAction} href={mapHref}>
                Abrir mapa
                <MapPin aria-hidden="true" size={15} />
              </Link>
            ) : null}
            {canViewAssets ? (
              <Link className={styles.primaryAction} href={inventoryHref}>
                Abrir inventario
                <ExternalLink aria-hidden="true" size={15} />
              </Link>
            ) : null}
          </>
        }
        description="Inventario municipal y órdenes de mantenimiento conectados a la organización seleccionada."
        eyebrow="Patrimonio operativo"
        title="Instalaciones"
      />

      <div className={styles.compactMetrics}>
        <Metric
          detail="Elementos no archivados"
          icon={Database}
          label="Activos registrados"
          value={permissionValue(canViewAssets, assets, errors.assets)}
        />
        <Metric
          detail="Planificadas, programadas o en curso"
          icon={Hammer}
          label="Órdenes abiertas"
          value={permissionValue(
            canViewMaintenance,
            maintenance,
            errors.maintenance,
          )}
        />
      </div>

      <div className={styles.facilitiesGrid}>
        <section className={styles.card}>
          <div className={styles.cardHeading}>
            <div>
              <p>Inventario</p>
              <h2>Activos municipales</h2>
            </div>
            {canViewAssets ? (
              <Link href={inventoryHref}>Ver todos</Link>
            ) : null}
          </div>

          {!canViewAssets ? (
            <ResourceState
              description="Tu cuenta no dispone del permiso assets.view en esta organización."
              icon={ShieldCheck}
              title="Inventario no autorizado"
              tone="restricted"
            />
          ) : errors.assets ? (
            <ResourceState
              action={
                <button
                  className={styles.secondaryAction}
                  onClick={onRetry}
                  type="button"
                >
                  Reintentar
                </button>
              }
              description={errors.assets}
              icon={CircleAlert}
              title="Inventario no disponible"
              tone="error"
            />
          ) : assets && assets.items.length > 0 ? (
            <div className={styles.assetList}>
              {assets.items.slice(0, 6).map((asset) => (
                <article key={asset.id}>
                  <span className={styles.assetIcon}>
                    <Database aria-hidden="true" size={17} strokeWidth={1.6} />
                  </span>
                  <div>
                    <Link
                      href={
                        canViewMap && asset.location_id
                          ? withOrganization(
                              `/mapa?entity_type=asset&entity_id=${asset.id}`,
                              organizationId,
                            )
                          : inventoryHref
                      }
                    >
                      {asset.name}
                    </Link>
                    <p>
                      {asset.asset_type.category.name} · {asset.asset_type.name}
                    </p>
                    <div className={styles.itemMeta}>
                      <span>{ASSET_STATUS_LABELS[asset.status]}</span>
                      <span>{ASSET_CONDITION_LABELS[asset.condition_status]}</span>
                      {asset.code ? <span>{asset.code}</span> : null}
                    </div>
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <ResourceState
              action={
                <div className={styles.inlineActions}>
                  <Link className={styles.primaryAction} href={inventoryHref}>
                    Configurar inventario
                  </Link>
                  {canViewMap ? (
                    <Link className={styles.secondaryAction} href={mapHref}>
                      Revisar mapa
                    </Link>
                  ) : null}
                </div>
              }
              description="Todavía no se han registrado edificios, redes, mobiliario u otros activos. El módulo está listo para recibir el inventario real."
              icon={Database}
              title="Inventario municipal sin datos"
            />
          )}
        </section>

        <section className={styles.card}>
          <div className={styles.cardHeading}>
            <div>
              <p>Mantenimiento</p>
              <h2>Trabajo abierto</h2>
            </div>
            {canViewMaintenance ? (
              <Link href={maintenanceHref}>Ver órdenes</Link>
            ) : null}
          </div>

          {!canViewMaintenance ? (
            <ResourceState
              description="Tu cuenta no dispone del permiso maintenance.view en esta organización."
              icon={ShieldCheck}
              title="Mantenimiento no autorizado"
              tone="restricted"
            />
          ) : errors.maintenance ? (
            <ResourceState
              action={
                <button
                  className={styles.secondaryAction}
                  onClick={onRetry}
                  type="button"
                >
                  Reintentar
                </button>
              }
              description={errors.maintenance}
              icon={CircleAlert}
              title="Mantenimiento no disponible"
              tone="error"
            />
          ) : (
            <>
            <div className={styles.dueFilter} role="group" aria-label="Vencimiento">
              {DUE_FILTERS.map(({ id, label }) => (
                <button
                  aria-pressed={dueFilter === id}
                  key={id}
                  onClick={() => setDueFilter(id)}
                  type="button"
                >
                  {label}
                </button>
              ))}
            </div>
            {dueError ? <p className="form-error">{dueError}</p> : null}
            </>
          )}
          {!canViewMaintenance || errors.maintenance ? null : shownOrders.length >
            0 ? (
            <div className={styles.maintenanceList}>
              {shownOrders.slice(0, 6).map((order) => (
                <article key={order.id}>
                  <div className={styles.itemHeading}>
                    <div>
                      <span>{MAINTENANCE_PRIORITY_LABELS[order.priority]}</span>
                      <h3>{order.title}</h3>
                    </div>
                    <span
                      className={styles.statusPill}
                      data-tone={order.status}
                    >
                      {MAINTENANCE_STATUS_LABELS[order.status]}
                    </span>
                  </div>
                  <p>{order.asset.name}</p>
                  <div className={styles.itemMeta}>
                    <span
                      className={
                        isOverdue(order) ? styles.overdueDate : undefined
                      }
                    >
                      {order.scheduled_for
                        ? `${isOverdue(order) ? "Vencida" : "Prevista"}: ${formatDate(order.scheduled_for)}`
                        : "Pendiente de programar"}
                    </span>
                    {order.assigned_to ? (
                      <span>{order.assigned_to.full_name}</span>
                    ) : (
                      <span>Sin responsable</span>
                    )}
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <ResourceState
              action={
                <Link className={styles.primaryAction} href={maintenanceHref}>
                  Abrir mantenimiento
                </Link>
              }
              description={
                dueFilter === "overdue"
                  ? "Ninguna orden abierta ha pasado de su fecha prevista."
                  : dueFilter === "soon"
                    ? "Ninguna orden abierta vence en los próximos 30 días."
                    : "No hay órdenes planificadas, programadas o en curso. Las nuevas actuaciones aparecerán aquí vinculadas a su activo."
              }
              icon={CheckCircle2}
              title="Sin mantenimiento pendiente"
            />
          )}
        </section>
      </div>
    </div>
  );
}

function PeopleTab({ organization }: { organization: Organization }) {
  return (
    <div className={styles.tabContent}>
      <SectionHeading
        description="Personas con acceso a la organización municipal. Este directorio no presupone una relación laboral."
        eyebrow="Organización"
        title="Personal y colaboradores"
      />

      <section className={styles.card}>
        <div className={styles.cardHeading}>
          <div>
            <p>Directorio real</p>
            <h2>{organization.users.length} personas</h2>
          </div>
          <span className={styles.statusPill} data-tone={organization.status}>
            {organization.status === "active"
              ? "Organización activa"
              : organization.status === "paused"
                ? "Organización pausada"
                : "Organización archivada"}
          </span>
        </div>
        {organization.description ? (
          <p className={styles.organizationDescription}>
            {organization.description}
          </p>
        ) : null}

        {organization.users.length > 0 ? (
          <div className={styles.peopleGrid}>
            {organization.users.map((member) => (
              <article key={member.id}>
                <span className={styles.avatar} aria-hidden="true">
                  {getInitials(member.full_name)}
                </span>
                <div>
                  <h3>{member.full_name}</h3>
                  <a href={`mailto:${member.email}`}>{member.email}</a>
                  <div className={styles.itemMeta}>
                    <span>{member.is_active ? "Acceso activo" : "Acceso inactivo"}</span>
                    {member.is_superuser ? <span>Administración global</span> : null}
                  </div>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <ResourceState
            description="La organización no tiene personas asociadas en este momento."
            icon={Users}
            title="Directorio vacío"
          />
        )}
      </section>

      <NotConfigured
        description="Puestos, contratos, horarios, ausencias, productividad, facturas y expedientes laborales requieren un dominio de RR. HH. separado, con permisos y conservación específicos."
        icon={UserRound}
        title="Gestión de recursos humanos"
        wide
      />
    </div>
  );
}

function RoadmapTab({
  user,
  ordinances,
  assets,
  maintenance,
  canViewMap,
  organizationId,
}: {
  user: User;
  ordinances: MunicipalCollection<Ordinance> | null;
  assets: MunicipalCollection<MunicipalAsset> | null;
  maintenance: MunicipalCollection<MaintenanceOrder> | null;
  canViewMap: boolean;
  organizationId: number;
}) {
  const shortcuts = [
    ...(shouldShowRequirementsPanel(user)
      ? [
          {
            href: "/requisitos",
            title: "Necesidades",
            description: "Priorizar demandas y convertirlas en trabajo revisable.",
            icon: CircleAlert,
          },
        ]
      : []),
    ...(shouldShowProjectsPanel(user)
      ? [
          {
            href: "/proyectos",
            title: "Proyectos",
            description: "Seguir iniciativas, responsables y documentación.",
            icon: FileStack,
          },
        ]
      : []),
    ...(canViewMap
      ? [
          {
            href: withOrganization("/mapa", organizationId),
            title: "Mapa municipal",
            description: "Situar necesidades, proyectos y activos sobre el territorio.",
            icon: MapPin,
          },
        ]
      : []),
    ...(userHasPermission(user, "assistant.use")
      ? [
          {
            href: "/asistente",
            title: "Anacleto",
            description: "Preparar borradores y ordenar próximos pasos con supervisión.",
            icon: ShieldCheck,
          },
        ]
      : []),
  ];

  return (
    <div className={styles.tabContent}>
      <SectionHeading
        description="Una vista de planificación deberá reunir objetivos, hitos, responsables, dependencias y resultados aprobados."
        eyebrow="Planificación"
        title="Hoja de ruta municipal"
      />

      <NotConfigured
        description="No existe todavía una entidad persistente de hoja de ruta. Para evitar compromisos ficticios, esta vista no transforma automáticamente proyectos o necesidades en un plan aprobado."
        icon={ClipboardList}
        title="Plan municipal no configurado"
        wide
      />

      <section className={styles.card}>
        <SectionHeading
          description="Módulos operativos que ya contienen información trazable y pueden alimentar la futura planificación."
          eyebrow="Fuentes disponibles"
          title="Trabajo conectado"
        />
        <div className={styles.areaGrid}>
          {shortcuts.map(({ href, title, description, icon: Icon }) => (
            <Link href={href} key={href}>
              <Icon aria-hidden="true" size={20} strokeWidth={1.6} />
              <span>
                <strong>{title}</strong>
                <small>{description}</small>
              </span>
            </Link>
          ))}
        </div>
        <div className={styles.roadmapSignals}>
          <div>
            <BookOpen aria-hidden="true" size={17} />
            <span>Normas registradas</span>
            <strong>{ordinances?.total ?? "—"}</strong>
          </div>
          <div>
            <Database aria-hidden="true" size={17} />
            <span>Activos registrados</span>
            <strong>{assets?.total ?? "—"}</strong>
          </div>
          <div>
            <Hammer aria-hidden="true" size={17} />
            <span>Mantenimiento abierto</span>
            <strong>{maintenance?.total ?? "—"}</strong>
          </div>
        </div>
      </section>
    </div>
  );
>>>>>>> origin/servidor-main-backup
}

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

function MunicipalWorkspaceContent() {
  const { user, handleRequestError } = useSession();
<<<<<<< HEAD
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const activeTab = resolveWorkspaceTab(searchParams);
=======
  const [activeTab, setActiveTab] = useState<ActiveTab>("summary");
  const [isMenuEditorOpen, setIsMenuEditorOpen] = useState(false);
  const [isShieldTargeted, setIsShieldTargeted] = useState(false);
  // Tarjetas de epígrafe desplegadas y epígrafe que se está arrastrando. Es
  // estado de presentación, no de selección: la URL sigue llevando la pestaña.
  const [openEpigraphIds, setOpenEpigraphIds] = useState<number[]>([]);
  const [draggedEpigraphId, setDraggedEpigraphId] = useState<number | null>(
    null,
  );
  // Solo guarda una elección explícita del selector. La organización que vale
  // es la de `selectedContext`, que cae en la primera cuando no se ha elegido:
  // con una sola organización el selector no se pinta y este estado se queda a
  // null para siempre.
>>>>>>> origin/servidor-main-backup
  const [selectedOrganizationId, setSelectedOrganizationId] = useState<
    number | null
  >(null);
  const contexts = user ? getMunicipalContexts(user) : [];
  const selectedContext =
    contexts.find(
      ({ organization: item }) => item.id === selectedOrganizationId,
    ) ??
    contexts[0] ??
    null;
  const activeOrganizationId = selectedContext?.organization.id ?? null;
  const townHallController = useTownHallController({
    handleRequestError,
    organizationId: activeOrganizationId ?? 0,
  });
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
  const [government, setGovernment] = useState<
    MunicipalCollection<GovernmentMember> | null
  >(null);
  const [staffWorkers, setStaffWorkers] = useState<
    MunicipalCollection<StaffWorker> | null
  >(null);
  const [staffPosts, setStaffPosts] = useState<
    MunicipalCollection<StaffPost> | null
  >(null);
  // Las series son un adorno informativo: si fallan, la ficha sigue en pie y
  // el bloque muestra su estado vacío, sin bandera de error propia.
  const [padron, setPadron] = useState<PadronRecord[]>([]);
  const [climate, setClimate] = useState<ClimateRecord[]>([]);
  const [households, setHouseholds] = useState<HouseholdStat[]>([]);
  const [officeHours, setOfficeHours] = useState<OfficeHour[]>([]);
  const [licences, setLicences] = useState<MunicipalLicence[]>([]);
  const [contracts, setContracts] = useState<MunicipalContract[]>([]);
  const [grants, setGrants] = useState<MunicipalGrant[]>([]);
  const [notices, setNotices] = useState<MunicipalNotice[]>([]);
  const [budgets, setBudgets] = useState<MunicipalBudget[]>([]);
  const [execution, setExecution] = useState<BudgetExecution | null>(null);
  const [movements, setMovements] = useState<TreasuryMovement[]>([]);
  const [sessions, setSessions] = useState<CouncilSession[]>([]);
  const [heritage, setHeritage] = useState<HeritageAsset[]>([]);
  const [archive, setArchive] = useState<ArchiveItem[]>([]);
  const [resourceErrors, setResourceErrors] = useState<ResourceErrors>(
    EMPTY_RESOURCE_ERRORS,
  );
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState("");
  const [loadAttempt, setLoadAttempt] = useState(0);
  const requestSequenceRef = useRef(0);
  const tabButtonRefs = useRef<Array<HTMLButtonElement | null>>([]);
  // Pestaña a la que ya se le desplegó el primer epígrafe.
  const expandedTabRef = useRef<ActiveTab | null>(null);

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
  const canViewGovernment = Boolean(user && hasGovernmentAccess(user));
  const canViewStaff = Boolean(user && hasStaffAccess(user));
  const canViewAdministration = Boolean(
    user &&
      (userHasPermission(user, "administration.view") ||
        userHasPermission(user, "administration.manage")),
  );
  const canViewCommunications = Boolean(
    user &&
      (userHasPermission(user, "communications.view") ||
        userHasPermission(user, "communications.manage")),
  );
  const canViewBudgets = Boolean(
    user &&
      (userHasPermission(user, "budgets.view") ||
        userHasPermission(user, "budgets.manage")),
  );
  const canViewPlenos = Boolean(
    user &&
      (userHasPermission(user, "plenos.view") ||
        userHasPermission(user, "plenos.manage")),
  );
  const canViewHeritage = Boolean(
    user &&
      (userHasPermission(user, "heritage.view") ||
        userHasPermission(user, "heritage.manage")),
  );
  const canManageOrdinances = Boolean(
    user &&
      ORDINANCE_MANAGEMENT_PERMISSIONS.some((permission) =>
        userHasPermission(user, permission),
      ),
  );

  useEffect(() => {
<<<<<<< HEAD
=======
    const requestedTab = new URLSearchParams(window.location.search).get("tab");
    if (requestedTab === null) {
      return;
    }
    // Los apartados propios se aceptan por forma: el árbol todavía no ha
    // llegado cuando se lee la URL.
    if (
      TAB_DEFINITIONS.some(({ id }) => id === requestedTab) ||
      isCustomTab(requestedTab)
    ) {
      setActiveTab(requestedTab as ActiveTab);
    }
  }, []);

  // Perfil del municipio y menú configurable: se cargan aparte de los módulos
  // operativos, para que un fallo en uno no arrastre al otro.
  useEffect(() => {
    if (!user || !canViewMunicipalHub(user) || activeOrganizationId === null) {
      return;
    }

    void townHallController.loadTownHall();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id, activeOrganizationId]);

  const weatherEnabled = townHallController.townHall?.profile.weather_enabled;
  const weatherLocation = townHallController.townHall?.profile.weather_location;

  useEffect(() => {
    if (!weatherEnabled) {
      return;
    }

    void townHallController.loadWeather();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [weatherEnabled, weatherLocation]);

  // Cambia cuando el árbol se recarga con otras pestañas o epígrafes.
  const navSignature = (townHallController.townHall?.nav ?? [])
    .map(
      (section) =>
        `${section.id}:${section.items.map((item) => item.id).join("-")}`,
    )
    .join("|");

  // Al entrar en una pestaña se despliega su primer epígrafe: abrirla con todo
  // plegado no enseñaría nada. Solo una vez por pestaña, para no volver a
  // plegar lo que el usuario abra después de renombrar o reordenar.
  useEffect(() => {
    const townHall = townHallController.townHall;

    if (!isCustomTab(activeTab)) {
      expandedTabRef.current = null;
      setOpenEpigraphIds((current) => (current.length === 0 ? current : []));
      return;
    }

    const placement = findTabForBlock(townHall, activeTab);

    if (placement === null) {
      return;
    }

    // Un enlace antiguo podía apuntar a un epígrafe: hoy es una tarjeta, así
    // que se traduce a su pestaña, con esa tarjeta ya desplegada.
    if (placement.epigraphId !== null) {
      expandedTabRef.current = `block-${placement.sectionId}`;
      setOpenEpigraphIds([placement.epigraphId]);
      selectWorkspaceTab(`block-${placement.sectionId}`);
      return;
    }

    if (expandedTabRef.current === activeTab) {
      return;
    }

    expandedTabRef.current = activeTab;
    const section = townHall?.nav.find(({ id }) => id === placement.sectionId);
    const first = section?.items[0]?.id;
    setOpenEpigraphIds(first === undefined ? [] : [first]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, navSignature]);

  // Cada tarjeta desplegada trae su propio contenido; las plegadas no piden
  // nada. Un fallo deja la tarjeta sin contenido y con su botón de reintento,
  // así que no se vuelve a pedir solo.
  const openEpigraphKey = openEpigraphIds.join(",");

  useEffect(() => {
    for (const blockId of openEpigraphIds) {
      if (townHallController.contents[blockId] === undefined) {
        void townHallController.loadContent(blockId);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openEpigraphKey]);

  useEffect(() => {
>>>>>>> origin/servidor-main-backup
    if (!user || !canViewMunicipalHub(user) || !selectedContext) {
      requestSequenceRef.current += 1;
      setMunicipality(null);
      setOrganization(null);
      setOrdinances(null);
      setAssets(null);
      setMaintenance(null);
      setGovernment(null);
      setStaffWorkers(null);
      setStaffPosts(null);
      setPadron([]);
      setClimate([]);
      setHouseholds([]);
      setOfficeHours([]);
      setLicences([]);
      setContracts([]);
      setGrants([]);
      setNotices([]);
    setBudgets([]);
    setExecution(null);
    setMovements([]);
    setSessions([]);
    setHeritage([]);
    setArchive([]);
      setBudgets([]);
      setExecution(null);
      setMovements([]);
      setSessions([]);
    setHeritage([]);
    setArchive([]);
      setHeritage([]);
      setArchive([]);
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
    setGovernment(null);
    setStaffWorkers(null);
    setStaffPosts(null);
    setPadron([]);
    setClimate([]);
    setHouseholds([]);
    setOfficeHours([]);
    setLicences([]);
    setContracts([]);
    setGrants([]);
    setNotices([]);
    setBudgets([]);
    setExecution(null);
    setMovements([]);
    setSessions([]);
    setHeritage([]);
    setArchive([]);
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
        governmentResult,
        staffWorkerResult,
        staffPostResult,
        padronResult,
        climateResult,
        householdResult,
        officeHourResult,
        licenceResult,
        contractResult,
        grantResult,
        noticeResult,
        budgetResult,
        movementResult,
        sessionResult,
        heritageResult,
        archiveResult,
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
        canViewGovernment
          ? fetchGovernmentMembers(organizationId, controller.signal)
          : Promise.resolve(null),
        canViewStaff
          ? fetchStaffWorkers(organizationId, controller.signal)
          : Promise.resolve(null),
        canViewStaff
          ? fetchStaffPosts(organizationId, controller.signal)
          : Promise.resolve(null),
        fetchPadronSeries(organizationId, controller.signal),
        fetchClimateSeries(organizationId, undefined, controller.signal),
        fetchHouseholdSeries(organizationId, controller.signal),
        fetchOfficeHours(organizationId, controller.signal),
        fetchLicences(organizationId, controller.signal),
        fetchContracts(organizationId, controller.signal),
        fetchGrants(organizationId, controller.signal),
        fetchNotices(organizationId, controller.signal),
        fetchBudgets(organizationId, controller.signal),
        fetchTreasuryMovements(organizationId, controller.signal),
        fetchCouncilSessions(organizationId, controller.signal),
        fetchHeritageAssets(organizationId, controller.signal),
        fetchArchiveItems(organizationId, controller.signal),
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

      if (governmentResult.status === "fulfilled") {
        setGovernment(governmentResult.value);
      } else {
        handleRequestError(
          governmentResult.reason,
          (message) =>
            setResourceErrors((current) => ({
              ...current,
              government: message,
            })),
          "No se pudo cargar la corporación municipal.",
        );
      }

      // La plantilla y sus puestos comparten permiso y error: si una falla, la
      // sección de personal no puede dibujarse con garantías.
      if (staffWorkerResult.status === "fulfilled") {
        setStaffWorkers(staffWorkerResult.value);
      } else {
        handleRequestError(
          staffWorkerResult.reason,
          (message) =>
            setResourceErrors((current) => ({ ...current, staff: message })),
          "No se pudo cargar el personal del ayuntamiento.",
        );
      }

      if (staffPostResult.status === "fulfilled") {
        setStaffPosts(staffPostResult.value);
      } else {
        handleRequestError(
          staffPostResult.reason,
          (message) =>
            setResourceErrors((current) => ({ ...current, staff: message })),
          "No se pudo cargar la plantilla municipal.",
        );
      }

      // Las series no levantan bandera de error: sin permiso o sin datos el
      // bloque enseña su estado vacío, que dice lo mismo sin alarmar.
      if (padronResult.status === "fulfilled") {
        setPadron(padronResult.value.items);
      }
      if (climateResult.status === "fulfilled") {
        setClimate(climateResult.value.items);
      }
      if (householdResult.status === "fulfilled") {
        setHouseholds(householdResult.value.items);
      }
      if (officeHourResult.status === "fulfilled") {
        setOfficeHours(officeHourResult.value.items);
      }
      if (licenceResult.status === "fulfilled") {
        setLicences(licenceResult.value.items);
      }
      if (contractResult.status === "fulfilled") {
        setContracts(contractResult.value.items);
      }
      if (grantResult.status === "fulfilled") {
        setGrants(grantResult.value.items);
      }
      if (noticeResult.status === "fulfilled") {
        setNotices(noticeResult.value.items);
      }
      if (movementResult.status === "fulfilled") {
        setMovements(movementResult.value.items);
      }
      if (sessionResult.status === "fulfilled") {
        setSessions(sessionResult.value.items);
      }
      if (heritageResult.status === "fulfilled") {
        setHeritage(heritageResult.value.items);
      }
      if (archiveResult.status === "fulfilled") {
        setArchive(archiveResult.value.items);
      }
      if (budgetResult.status === "fulfilled") {
        setBudgets(budgetResult.value.items);
        // La ejecución se pide solo del ejercicio más reciente: es lo que la
        // ficha enseña, y calcularla para todos sería trabajo tirado.
        const latest = budgetResult.value.items[0];
        if (latest) {
          try {
            setExecution(
              await fetchBudgetExecution(latest.id, controller.signal),
            );
          } catch {
            setExecution(null);
          }
        }
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
  const municipalityLabel =
    townHallController.townHall?.profile.display_name?.trim() ||
    selectedContext.municipality.name;
  const townHall = townHallController.townHall;
  const canEditMenu = canEditTownHall(user);

  // Las pestañas del editor se añaden tras las áreas fijas, de modo que la
  // tira siga siendo una sola navegación. Ya no llevan desplegable: sus
  // epígrafes se apilan debajo, en tarjetas.
  const workspaceTabs: TabDefinition[] = [
    ...TAB_DEFINITIONS,
    ...(townHall?.nav ?? []).map((section) => ({
      id: `block-${section.id}` as CustomTab,
      label: section.title,
      icon: Landmark,
    })),
  ];
  const activeSection =
    townHall?.nav.find(({ id }) => `block-${id}` === activeTab) ?? null;

  function handleTabKeyDown(
    event: KeyboardEvent<HTMLButtonElement>,
    currentIndex: number,
  ) {
    let nextIndex: number | null = null;

    if (event.key === "ArrowRight") {
      nextIndex = (currentIndex + 1) % workspaceTabs.length;
    } else if (event.key === "ArrowLeft") {
      nextIndex =
        (currentIndex - 1 + workspaceTabs.length) % workspaceTabs.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = workspaceTabs.length - 1;
    }

    if (nextIndex === null) {
      return;
    }

    event.preventDefault();
    selectWorkspaceTab(workspaceTabs[nextIndex].id);
    tabButtonRefs.current[nextIndex]?.focus();
  }

<<<<<<< HEAD
  function selectWorkspaceTab(tab: WorkspaceTab, moveFocus = false) {
    router.replace(buildWorkspaceTabHref(pathname, searchParams, tab), {
      scroll: false,
    });
=======
  function selectWorkspaceTab(tab: ActiveTab, moveFocus = false) {
    setActiveTab(tab);
    const url = new URL(window.location.href);
    if (tab === "summary") {
      url.searchParams.delete("tab");
    } else {
      url.searchParams.set("tab", tab);
    }
    window.history.replaceState(window.history.state, "", url);
>>>>>>> origin/servidor-main-backup

    if (moveFocus) {
      const tabIndex = workspaceTabs.findIndex(({ id }) => id === tab);
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
    setGovernment(null);
    setStaffWorkers(null);
    setStaffPosts(null);
    setPadron([]);
    setClimate([]);
    setHouseholds([]);
    setOfficeHours([]);
    setLicences([]);
    setContracts([]);
    setGrants([]);
    setNotices([]);
    setBudgets([]);
    setExecution(null);
    setMovements([]);
    setSessions([]);
    setHeritage([]);
    setArchive([]);
    setResourceErrors(EMPTY_RESOURCE_ERRORS);
    setError("");
    setIsLoading(true);
    selectWorkspaceTab("summary");
  }

  function retryWorkspace() {
    setLoadAttempt((value) => value + 1);
  }

  function handleShieldDrop(event: DragEvent<HTMLSpanElement>) {
    event.preventDefault();
    setIsShieldTargeted(false);

    const file = event.dataTransfer.files?.[0];
    if (canEditMenu && file && file.type.startsWith("image/")) {
      void townHallController.uploadShield(file);
    }
  }

  function toggleEpigraph(blockId: number) {
    setOpenEpigraphIds((current) =>
      current.includes(blockId)
        ? current.filter((id) => id !== blockId)
        : [...current, blockId],
    );
  }

  // Reordenar por arrastre o por el menú acaba en el mismo sitio: el árbol
  // entero, que es lo que el backend guarda de una vez.
  function reorderEpigraph(draggedId: number, targetId: number) {
    if (townHall === null || activeSection === null) {
      return;
    }

    const next = moveEpigraph(
      townHall.nav,
      activeSection.id,
      draggedId,
      targetId,
    );

    if (next !== null) {
      void townHallController.reorderNav(next);
    }
  }

  function moveEpigraphBy(blockId: number, offset: number) {
    if (activeSection === null) {
      return;
    }

    const index = activeSection.items.findIndex(({ id }) => id === blockId);
    const target = activeSection.items[index + offset];

    if (target !== undefined) {
      reorderEpigraph(blockId, target.id);
    }
  }

  return (
    <section className={styles.workspace}>
      <header className={styles.masthead}>
        <div className={styles.identity}>
          {/* El escudo sustituye a las iniciales cuando se ha subido uno; se
              reemplaza soltando una imagen encima. */}
          <span
            aria-hidden="true"
            className={`${styles.municipalityMark}${
              isShieldTargeted ? ` ${styles.municipalityMarkTargeted}` : ""
            }`}
            onDragLeave={() => setIsShieldTargeted(false)}
            onDragOver={(event) => {
              if (!canEditMenu) {
                return;
              }
              event.preventDefault();
              setIsShieldTargeted(true);
            }}
            onDrop={handleShieldDrop}
            title={
              canEditMenu ? "Arrastra una imagen para cambiar el escudo" : undefined
            }
          >
            {townHall?.profile.has_shield ? (
              <img
                alt=""
                src={townHallShieldUrl(townHallController.shieldVersion)}
              />
            ) : (
              getInitials(municipalityLabel)
            )}
          </span>
          <div>
            <h1>{municipalityLabel}</h1>
            <span>
              {selectedContext.municipality.province} ·{" "}
              {selectedContext.municipality.autonomous_community}
            </span>
          </div>
        </div>

        <div className={styles.mastheadAside}>
          {/* El selector solo aparece cuando hay algo que elegir: con una sola
              organización el nombre ya está en el titular y la tarjeta sobraba. */}
          {contexts.length > 1 ? (
            <select
              aria-label="Organización y municipio"
              className={styles.orgSwitch}
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
          ) : null}

          <div className={styles.municipalBar}>
            {townHall?.profile.weather_enabled ? (
              <span
                className={styles.weatherBlock}
                title={
                  townHallController.weather
                    ? `Temperatura de hoy en ${townHallController.weather.location}`
                    : "Temperatura no disponible ahora mismo"
                }
              >
                <CloudSun aria-hidden="true" size={18} strokeWidth={1.6} />
                {/* Si el proveedor no responde se muestra un guion, nunca una
                    cifra inventada. */}
                <strong>
                  {townHallController.weather
                    ? `${Math.round(
                        townHallController.weather.temperature_celsius,
                      )}°C`
                    : "—"}
                </strong>
              </span>
            ) : null}
          </div>
        </div>
      </header>

      {/* Fila de pestañas del prototipo: plana, alineada a la izquierda y
          separada por un filete inferior, con el botón de gestión delante. No
          lleva desplegables: los epígrafes de la pestaña activa se apilan
          debajo en tarjetas. Ver docs/diseno-ayuntamiento-prototipo.md §8. */}
      <nav
        aria-label="Áreas del ayuntamiento"
        className={styles.areaTabs}
        role="tablist"
      >
        {canEditMenu ? (
          <button
            aria-label="Gestionar pestañas"
            className={styles.areaTabsManage}
            onClick={() => setIsMenuEditorOpen(true)}
            title="Gestionar pestañas"
            type="button"
          >
            {/* El prototipo abre las pestañas con el mismo asa de seis puntos
                que las tarjetas, no con un engranaje. */}
            <svg
              aria-hidden="true"
              fill="currentColor"
              height="14"
              viewBox="0 0 24 24"
              width="14"
            >
              <circle cx="9" cy="6" r="1.5" />
              <circle cx="15" cy="6" r="1.5" />
              <circle cx="9" cy="12" r="1.5" />
              <circle cx="15" cy="12" r="1.5" />
              <circle cx="9" cy="18" r="1.5" />
              <circle cx="15" cy="18" r="1.5" />
            </svg>
          </button>
        ) : null}
        {workspaceTabs.map(({ id, label, icon: Icon }, index) => (
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
            <Icon aria-hidden="true" size={16} strokeWidth={1.6} />
            <span>{label}</span>
          </button>
        ))}
      </nav>

      {isPaused ? (
        <p className={styles.warning} role="status">
          La organización está pausada. La información permanece disponible en
          modo de consulta.
        </p>
      ) : null}

      <div
        aria-labelledby={`municipal-tab-${activeTab}`}
        id={`municipal-panel-${activeTab}`}
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
            governmentSection={
              <EstructuraGobierno
                canView={canViewGovernment}
                error={resourceErrors.government}
                members={government}
                onRetry={retryWorkspace}
              />
            }
            maintenance={maintenance}
            municipality={municipality}
            administrationSection={
              <>
                <Administracion
                  canView={canViewAdministration}
                  contracts={contracts}
                  grants={grants}
                  licences={licences}
                  officeHours={officeHours}
                />
                <Comunicacion
                  canView={canViewCommunications}
                  notices={notices}
                />
                <Presupuestos
                  budgets={budgets}
                  canView={canViewBudgets}
                  execution={execution}
                  movements={movements}
                />
                <Plenos canView={canViewPlenos} sessions={sessions} />
                <Patrimonio
                  archive={archive}
                  assets={heritage}
                  canView={canViewHeritage}
                />
              </>
            }
            seriesSection={
              <SeriesMunicipio
                climate={climate}
                households={households}
                padron={padron}
              />
            }
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
        ) : activeTab === "map" ? (
          <MapaGeneral
            canViewMap={canViewMap}
            organizationId={selectedContext.organization.id}
          />
        ) : activeTab === "people" ? (
<<<<<<< HEAD
          <Personal
            canViewStaff={canViewStaff}
            errors={resourceErrors}
            onRetry={retryWorkspace}
            organization={organization}
            posts={staffPosts}
            workers={staffWorkers}
          />
        ) : (
          <HojaDeRuta
=======
          <PeopleTab organization={organization} />
        ) : activeSection !== null ? (
          <div className={styles.tabContent}>
            {activeSection.items.length === 0 ? (
              <section className={`panel ${styles.pageState}`}>
                <Landmark aria-hidden="true" size={28} strokeWidth={1.6} />
                <p className="eyebrow">{activeSection.title}</p>
                <h1>Sin epígrafes</h1>
                <p className="muted">
                  {canEditMenu
                    ? "Añade el primero desde «Gestionar pestañas», el botón que abre la fila."
                    : "Esta pestaña todavía no tiene contenido."}
                </p>
              </section>
            ) : (
              // Epígrafes apilados en tarjetas, como el prototipo: una por
              // epígrafe, plegables y reordenables por arrastre.
              <article className="townhall-epigraph-stack">
                {activeSection.items.map((item, index) => {
                  const content = townHallController.contents[item.id];
                  const isLoadingEpigraph =
                    townHallController.loadingContentIds.includes(item.id);

                  return (
                    <TownHallEpigraphCard
                      canEdit={canEditMenu}
                      canMoveDown={index < activeSection.items.length - 1}
                      canMoveUp={index > 0}
                      isOpen={openEpigraphIds.includes(item.id)}
                      isSaving={townHallController.isSavingTownHall}
                      key={item.id}
                      onDelete={() =>
                        void townHallController.archiveBlock(item.id)
                      }
                      onDragStart={() => setDraggedEpigraphId(item.id)}
                      onDrop={() => {
                        if (draggedEpigraphId !== null) {
                          reorderEpigraph(draggedEpigraphId, item.id);
                          setDraggedEpigraphId(null);
                        }
                      }}
                      onMoveDown={() => moveEpigraphBy(item.id, 1)}
                      onMoveUp={() => moveEpigraphBy(item.id, -1)}
                      onRename={(title) =>
                        void townHallController.renameBlock(item.id, title)
                      }
                      onToggle={() => toggleEpigraph(item.id)}
                      title={item.title}
                    >
                      {content !== undefined ? (
                        <TownHallContentPanel
                          canEdit={canEditMenu}
                          content={content}
                          embedded
                          isSaving={townHallController.isSavingTownHall}
                          onAdd={() =>
                            void townHallController.addContentItem(
                              item.id,
                              "Nuevo elemento",
                            )
                          }
                          onAddAttachment={(itemId, file) =>
                            void townHallController.addAttachment(
                              item.id,
                              itemId,
                              file,
                            )
                          }
                          onArchive={(itemId) =>
                            void townHallController.archiveContentItem(
                              item.id,
                              itemId,
                            )
                          }
                          onChangeLayout={(layout) =>
                            void townHallController.setSectionLayout(
                              item.id,
                              layout,
                            )
                          }
                          onRemoveAttachment={(itemId, attachmentIndex) =>
                            void townHallController.removeAttachment(
                              item.id,
                              itemId,
                              attachmentIndex,
                            )
                          }
                          onSaveBody={(itemId, body) =>
                            void townHallController.saveContentItem(
                              item.id,
                              itemId,
                              { body: body.trim() === "" ? null : body },
                            )
                          }
                          onSaveFields={(itemId, fields) =>
                            void townHallController.saveContentFields(
                              item.id,
                              itemId,
                              fields,
                            )
                          }
                          onSavePoints={(itemId, raw) =>
                            void townHallController.saveContentPoints(
                              item.id,
                              itemId,
                              parseSeriesPoints(raw),
                            )
                          }
                          onSaveTitle={(itemId, title) =>
                            void townHallController.saveContentItem(
                              item.id,
                              itemId,
                              { title },
                            )
                          }
                        />
                      ) : isLoadingEpigraph ? (
                        <p
                          aria-live="polite"
                          className="townhall-epigraph-state"
                          role="status"
                        >
                          Cargando el contenido…
                        </p>
                      ) : (
                        <div className="townhall-epigraph-state">
                          <p>No se pudo cargar el contenido de este epígrafe.</p>
                          <button
                            className={styles.primaryAction}
                            onClick={() =>
                              void townHallController.loadContent(item.id)
                            }
                            type="button"
                          >
                            Reintentar
                          </button>
                        </div>
                      )}
                    </TownHallEpigraphCard>
                  );
                })}
              </article>
            )}
          </div>
        ) : activeTab === "roadmap" ? (
          <RoadmapTab
>>>>>>> origin/servidor-main-backup
            assets={assets}
            canViewMap={canViewMap}
            maintenance={maintenance}
            ordinances={ordinances}
            organizationId={selectedContext.organization.id}
            user={user}
          />
        ) : (
          // Pestaña propia cuyo árbol todavía no ha llegado: la barra ya está
          // pintada, así que solo falta decir que se está trayendo.
          <div
            aria-busy="true"
            aria-live="polite"
            className={styles.loadingState}
            role="status"
          >
            <RefreshCw aria-hidden="true" size={22} />
            <div>
              <strong>Cargando la pestaña</strong>
              <span>Recuperando sus epígrafes…</span>
            </div>
          </div>
        )}
      </div>

      {canEditMenu && isMenuEditorOpen && townHall !== null ? (
        <TownHallNavEditor
          fallbackName={selectedContext.municipality.name}
          isSaving={townHallController.isSavingTownHall}
          onAddItem={(sectionId) =>
            void townHallController.addItem(sectionId, "Nuevo epígrafe")
          }
          onAddSection={() => void townHallController.addSection("Nueva pestaña")}
          onArchiveBlock={(blockId) =>
            void townHallController.archiveBlock(blockId)
          }
          onChangeWeatherLocation={(location) =>
            void townHallController.updateProfile({
              weather_location: location === "" ? null : location,
            })
          }
          onClose={() => setIsMenuEditorOpen(false)}
          onRenameBlock={(blockId, title) =>
            void townHallController.renameBlock(blockId, title)
          }
          onRenameMunicipality={(name) =>
            void townHallController.updateProfile({
              display_name: name === "" ? null : name,
            })
          }
          onReorder={(nav) => void townHallController.reorderNav(nav)}
          onToggleWeather={(enabled) =>
            void townHallController.updateProfile({ weather_enabled: enabled })
          }
          townHall={townHall}
        />
      ) : null}
    </section>
  );
}

// `useSearchParams` obliga a una frontera de Suspense en el App Router: sin
// ella el build de producción falla al prerenderizar la ruta.
export function MunicipalWorkspace() {
  return (
    <Suspense fallback={null}>
      <MunicipalWorkspaceContent />
    </Suspense>
  );
}
