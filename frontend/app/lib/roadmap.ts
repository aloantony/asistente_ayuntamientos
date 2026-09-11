import type {
  GovernmentMember,
  MunicipalTask,
  MunicipalTaskSummary,
} from "../components/types";
import { adminRequest, adminRequestWithTotal } from "./api";
import type { MunicipalCollection } from "./municipalWorkspace";

export type RoadmapTaskFilter = {
  /** Sin estado, la lista trae solo lo abierto. */
  status?: MunicipalTask["status"];
  overdue?: boolean;
  unassigned?: boolean;
  includeClosed?: boolean;
};

/** Chip activo de la banda de filtros. `todas` no impone ninguna condición. */
export type RoadmapFilterKey =
  | "todas"
  | "in_progress"
  | "blocked"
  | "overdue"
  | "unassigned"
  | "completed";

export const ROADMAP_FILTERS: {
  key: RoadmapFilterKey;
  label: string;
  filter: RoadmapTaskFilter;
}[] = [
  { key: "todas", label: "Todas", filter: { includeClosed: true } },
  {
    key: "in_progress",
    label: "En curso",
    filter: { status: "in_progress" },
  },
  { key: "blocked", label: "Bloqueadas", filter: { status: "blocked" } },
  { key: "overdue", label: "Vencidas", filter: { overdue: true } },
  { key: "unassigned", label: "Sin asignar", filter: { unassigned: true } },
  {
    key: "completed",
    label: "Completadas",
    filter: { status: "completed", includeClosed: true },
  },
];

export function fetchTaskSummary(
  organizationId: number,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    organization_id: String(organizationId),
  });

  return adminRequest<MunicipalTaskSummary>(
    `/tasks/summary?${params.toString()}`,
    "",
    "No se pudo cargar el resumen de la hoja de ruta.",
    { signal },
  );
}

export const ROADMAP_PAGE_SIZE = 50;

export function fetchTasks(
  organizationId: number,
  filter: RoadmapTaskFilter = {},
  signal?: AbortSignal,
  offset = 0,
) {
  const params = new URLSearchParams({
    organization_id: String(organizationId),
    limit: String(ROADMAP_PAGE_SIZE),
    offset: String(offset),
  });
  if (filter.status) {
    params.set("status", filter.status);
  }
  if (filter.overdue) {
    params.set("overdue", "true");
  }
  if (filter.unassigned) {
    params.set("unassigned", "true");
  }
  if (filter.includeClosed) {
    params.set("include_closed", "true");
  }

  return adminRequestWithTotal<MunicipalTask[]>(
    `/tasks?${params.toString()}`,
    "",
    "No se pudo cargar la hoja de ruta municipal.",
    { signal },
  );
}

export function fetchRoadmapGovernment(
  organizationId: number,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    organization_id: String(organizationId),
    include_archived: "false",
    limit: "200",
  });

  return adminRequestWithTotal<GovernmentMember[]>(
    `/government/members?${params.toString()}`,
    "",
    "No se pudo cargar la corporación municipal.",
    { signal },
  );
}

export type RoadmapTasks = MunicipalCollection<MunicipalTask>;
