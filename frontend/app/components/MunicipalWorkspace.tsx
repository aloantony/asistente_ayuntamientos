"use client";

import {
  BookOpen,
  Building2,
  CircleAlert,
  ClipboardList,
  CloudSun,
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
import {
  canViewGovernment as hasGovernmentAccess,
  canViewMunicipalHub,
  canViewStaff as hasStaffAccess,
} from "../lib/permissions";
import { canEditTownHall, useSession } from "../lib/session";
import styles from "./MunicipalWorkspace.module.css";
import { townHallShieldUrl } from "../lib/api";
import { useTownHallController } from "../lib/useTownHallController";
import { TownHallContentPanel } from "./TownHallContentPanel";
import { TownHallEpigraphCard } from "./TownHallEpigraphCard";
import { TownHallNavEditor } from "./TownHallNavEditor";
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
  type StaffPost,
  type StaffWorker,
  type TownHall,
  type TownHallNavSection,
  type User,
} from "./types";

// Las áreas fijas conservan sus claves literales (ADR-048); los apartados que
// el usuario crea en el editor del menú se identifican con el id de su bloque
// (ADR-034). Ambas viven en la misma tira de pestañas: ver ADR-052.
type CustomTab = `block-${number}`;
type ActiveTab = WorkspaceTab | CustomTab;

type TabDefinition = {
  id: ActiveTab;
  label: string;
  icon: LucideIcon;
};

function isCustomTab(tab: string): tab is CustomTab {
  return /^block-\d+$/.test(tab);
}

/** Localiza la pestaña de un bloque: si es un epígrafe, la de su pestaña padre.
 *  Sirve para que los enlaces antiguos a un epígrafe sigan llevando a su sitio,
 *  ahora que un epígrafe es una tarjeta dentro de una pestaña y no una pestaña. */
function findTabForBlock(townHall: TownHall | null, tab: ActiveTab) {
  if (townHall === null || !isCustomTab(tab)) {
    return null;
  }

  const blockId = Number(tab.slice("block-".length));
  for (const section of townHall.nav) {
    if (section.id === blockId) {
      return { sectionId: section.id, epigraphId: null as number | null };
    }
    if (section.epigraphs.some((candidate) => candidate.id === blockId)) {
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
  const from = section?.epigraphs.findIndex((item) => item.id === draggedId) ?? -1;
  const to = section?.epigraphs.findIndex((item) => item.id === targetId) ?? -1;

  if (section === undefined || from === -1 || to === -1) {
    return null;
  }

  const epigraphs = [...section.epigraphs];
  const [moved] = epigraphs.splice(from, 1);
  epigraphs.splice(to, 0, moved);

  return nav.map((candidate) =>
    candidate.id === sectionId ? { ...candidate, epigraphs } : candidate,
  );
}

// Rótulos tomados de la navegación municipal de referencia. Los identificadores
// no cambian: se usan en enlaces `?tab=` repartidos por el producto.
const TAB_DEFINITIONS: TabDefinition[] = [
  { id: "summary", label: "Información", icon: Landmark },
  { id: "ordinances", label: "Normativa", icon: BookOpen },
  { id: "facilities", label: "Servicios municipales", icon: Wrench },
  { id: "map", label: "Mapa general", icon: MapIcon },
  { id: "people", label: "Personal", icon: Users },
  { id: "roadmap", label: "Hoja de ruta", icon: ClipboardList },
];

/** Convierte el texto libre del editor de series en puntos. Una línea por
 *  punto, «etiqueta: valor»; lo que no encaje se descarta en silencio. */
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

function isWorkspaceTab(value: string | null): value is WorkspaceTab {
  return TAB_DEFINITIONS.some(({ id }) => id === value);
}

// La pestaña vive en la URL, no en estado local: así un enlace `?tab=` abre
// donde dice, el botón atrás funciona y el rótulo no se queda desincronizado.
export function resolveWorkspaceTab(
  searchParams: Pick<URLSearchParams, "get">,
): ActiveTab {
  const requestedTab = searchParams.get("tab");
  if (isWorkspaceTab(requestedTab)) {
    return requestedTab;
  }
  // Los apartados propios se aceptan por su forma: el árbol del menú todavía
  // no ha llegado cuando se lee la URL. Si luego resulta que no existe, el
  // panel lo dice; no se puede validar aquí.
  return requestedTab !== null && isCustomTab(requestedTab)
    ? requestedTab
    : "summary";
}

export function buildWorkspaceTabHref(
  pathname: string,
  searchParams: Pick<URLSearchParams, "toString">,
  tab: ActiveTab,
) {
  const params = new URLSearchParams(searchParams.toString());
  // «summary» es el valor por defecto: no se escribe en la URL para que la
  // dirección de la pantalla inicial quede limpia.
  if (tab === "summary") {
    params.delete("tab");
  } else {
    params.set("tab", tab);
  }
  const query = params.toString();
  return query ? `${pathname}?${query}` : pathname;
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
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const activeTab = resolveWorkspaceTab(searchParams);
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
  const [isMenuEditorOpen, setIsMenuEditorOpen] = useState(false);
  const [isShieldTargeted, setIsShieldTargeted] = useState(false);
  // Tarjetas de epígrafe desplegadas y epígrafe que se está arrastrando. Es
  // estado de presentación, no de selección: la URL sigue llevando la pestaña.
  const [openEpigraphIds, setOpenEpigraphIds] = useState<number[]>([]);
  // Qué apartado enseña cada epígrafe. Sin entrada, el primero: abrir una
  // tarjeta tiene que enseñar algo (ADR-054).
  const [openApartadoIds, setOpenApartadoIds] = useState<Record<number, number>>(
    {},
  );
  const [draggedEpigraphId, setDraggedEpigraphId] = useState<number | null>(
    null,
  );
  // Pestaña a la que ya se le desplegó el primer epígrafe.
  const expandedTabRef = useRef<ActiveTab | null>(null);

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
    organizationId: activeOrganizationId,
  });
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

  // Perfil del municipio y menú configurable: se cargan aparte de los módulos
  // operativos, para que un fallo en uno no arrastre al otro (ADR-034).
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
        `${section.id}:${section.epigraphs
          .map((epigraph) => `${epigraph.id}.${epigraph.items.map((item) => item.id).join("+")}`)
          .join("-")}`,
    )
    .join("|");

  // Al entrar en una pestaña propia se despliega su primer epígrafe: abrirla
  // con todo plegado no enseñaría nada. Solo una vez por pestaña, para no
  // volver a plegar lo que el usuario abra después de renombrar o reordenar.
  useEffect(() => {
    const currentTownHall = townHallController.townHall;

    if (!isCustomTab(activeTab)) {
      expandedTabRef.current = null;
      setOpenEpigraphIds((current) => (current.length === 0 ? current : []));
      return;
    }

    const placement = findTabForBlock(currentTownHall, activeTab);

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
    const section = currentTownHall?.nav.find(
      ({ id }) => id === placement.sectionId,
    );
    const first = section?.epigraphs[0]?.id;
    setOpenEpigraphIds(first === undefined ? [] : [first]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, navSignature]);

  // Cada tarjeta desplegada trae su propio contenido; las plegadas no piden
  // nada. Un fallo deja la tarjeta sin contenido y con su botón de reintento,
  // así que no se vuelve a pedir solo.
  const activeSectionForContent = (townHallController.townHall?.nav ?? []).find(
    (section) => `block-${section.id}` === activeTab,
  );
  const openApartadoIdList = (activeSectionForContent?.epigraphs ?? [])
    .filter((epigraph) => openEpigraphIds.includes(epigraph.id))
    .map(
      (epigraph) => openApartadoIds[epigraph.id] ?? epigraph.items[0]?.id,
    )
    .filter((id): id is number => id !== undefined);
  const openApartadoKey = openApartadoIdList.join(",");

  useEffect(() => {
    for (const blockId of openApartadoIdList) {
      if (townHallController.contents[blockId] === undefined) {
        void townHallController.loadContent(blockId);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openApartadoKey]);

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
  const townHall = townHallController.townHall;
  const canEditMenu = canEditTownHall(user);
  const municipalityLabel =
    townHall?.profile.display_name?.trim() || selectedContext.municipality.name;

  // Las pestañas del editor se añaden tras las áreas fijas, de modo que la
  // tira siga siendo una sola navegación (ADR-052).
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
  const activeTabDefinition =
    workspaceTabs.find((tab) => tab.id === activeTab) ?? workspaceTabs[0];

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

  function selectWorkspaceTab(tab: ActiveTab, moveFocus = false) {
    router.replace(buildWorkspaceTabHref(pathname, searchParams, tab), {
      scroll: false,
    });

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

    const index = activeSection.epigraphs.findIndex(({ id }) => id === blockId);
    const target = activeSection.epigraphs[index + offset];

    if (target !== undefined) {
      reorderEpigraph(blockId, target.id);
    }
  }

  function retryWorkspace() {
    setLoadAttempt((value) => value + 1);
  }

  return (
    <section className={styles.workspace}>
      <header className={styles.masthead}>
        <div className={styles.identity}>
          {/* El escudo sustituye a las iniciales cuando se ha subido uno; se
              reemplaza soltando una imagen encima (ADR-034). */}
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
              canEditMenu
                ? "Arrastra una imagen para cambiar el escudo"
                : undefined
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
            <p>Espacio municipal</p>
            <h1>{municipalityLabel}</h1>
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
                  cifra inventada (ADR-034). */}
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
      </header>

      {isPaused ? (
        <p className={styles.warning} role="status">
          La organización está pausada. La información permanece disponible en
          modo de consulta.
        </p>
      ) : null}

      <nav aria-label="Áreas del ayuntamiento" className={styles.tabs} role="tablist">
        {canEditMenu ? (
          <button
            aria-label="Gestionar pestañas"
            className={styles.tabsManage}
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
            canViewOrdinances={canViewOrdinances}
            governmentSection={
              <EstructuraGobierno
                canView={canViewGovernment}
                error={resourceErrors.government}
                members={government}
                onRetry={retryWorkspace}
              />
            }
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
          <Personal
            canViewStaff={canViewStaff}
            errors={resourceErrors}
            onRetry={retryWorkspace}
            organization={organization}
            posts={staffPosts}
            workers={staffWorkers}
          />
        ) : activeTab === "roadmap" ? (
          <HojaDeRuta
            assets={assets}
            canViewMap={canViewMap}
            maintenance={maintenance}
            ordinances={ordinances}
            organizationId={selectedContext.organization.id}
            user={user}
          />
        ) : activeSection !== null ? (
          <div className={styles.tabContent}>
            {activeSection.epigraphs.length === 0 ? (
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
                {activeSection.epigraphs.map((epigraph, index) => {
                  // El contenido vive en el apartado —la pestaña interna del
                  // diseño—, no en el epígrafe: la tarjeta enseña el abierto.
                  const apartado =
                    epigraph.items.find(
                      ({ id }) => id === openApartadoIds[epigraph.id],
                    ) ?? epigraph.items[0];
                  const item = apartado ?? epigraph;
                  const content =
                    apartado === undefined
                      ? undefined
                      : townHallController.contents[apartado.id];
                  const isLoadingEpigraph =
                    apartado !== undefined &&
                    townHallController.loadingContentIds.includes(apartado.id);

                  return (
                    <TownHallEpigraphCard
                      canEdit={canEditMenu}
                      canMoveDown={index < activeSection.epigraphs.length - 1}
                      canMoveUp={index > 0}
                      isOpen={openEpigraphIds.includes(epigraph.id)}
                      isSaving={townHallController.isSavingTownHall}
                      key={epigraph.id}
                      onDelete={() =>
                        void townHallController.archiveBlock(epigraph.id)
                      }
                      onDragStart={() => setDraggedEpigraphId(epigraph.id)}
                      onDrop={() => {
                        if (draggedEpigraphId !== null) {
                          reorderEpigraph(draggedEpigraphId, epigraph.id);
                          setDraggedEpigraphId(null);
                        }
                      }}
                      onMoveDown={() => moveEpigraphBy(epigraph.id, 1)}
                      onMoveUp={() => moveEpigraphBy(epigraph.id, -1)}
                      onRename={(title) =>
                        void townHallController.renameBlock(epigraph.id, title)
                      }
                      onToggle={() => toggleEpigraph(epigraph.id)}
                      title={epigraph.title}
                    >
                      {epigraph.items.length > 1 ? (
                        // Las pestañas internas del epígrafe, como el diseño.
                        <div
                          aria-label={`Apartados de ${epigraph.title}`}
                          className="townhall-epigraph-tabs"
                          role="tablist"
                        >
                          {epigraph.items.map((apartadoTab) => (
                            <button
                              aria-selected={apartadoTab.id === item.id}
                              className={
                                apartadoTab.id === item.id
                                  ? "townhall-epigraph-tab is-active"
                                  : "townhall-epigraph-tab"
                              }
                              key={apartadoTab.id}
                              onClick={() =>
                                setOpenApartadoIds((current) => ({
                                  ...current,
                                  [epigraph.id]: apartadoTab.id,
                                }))
                              }
                              role="tab"
                              type="button"
                            >
                              {apartadoTab.title}
                            </button>
                          ))}
                        </div>
                      ) : null}
                      {apartado === undefined ? (
                        <p className="townhall-epigraph-state">
                          Este epígrafe todavía no tiene apartados.
                        </p>
                      ) : content !== undefined ? (
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
            void townHallController.addEpigraph(sectionId, "Nuevo epígrafe")
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
