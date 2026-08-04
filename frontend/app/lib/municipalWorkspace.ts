import type {
  GovernmentMember,
  MaintenanceOrder,
  MunicipalAsset,
  Organization,
  Ordinance,
  StaffAbsence,
  StaffInvoice,
  StaffPost,
  StaffReport,
  StaffWorker,
} from "../components/types";
import { adminRequest, adminRequestWithTotal } from "./api";

export type MunicipalCollection<T> = {
  items: T[];
  total: number;
};

export function fetchMunicipalOrganization(
  organizationId: number,
  signal?: AbortSignal,
) {
  return adminRequest<Organization>(
    `/organizations/${organizationId}`,
    "",
    "No se pudo cargar el directorio de la organización.",
    { signal },
  );
}

export function fetchMunicipalOrdinances(
  municipalityId: number,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    municipality_id: String(municipalityId),
    include_archived: "false",
    limit: "200",
  });

  return adminRequestWithTotal<Ordinance[]>(
    `/ordinances?${params.toString()}`,
    "",
    "No se pudo cargar el repositorio normativo municipal.",
    { signal },
  );
}

export function fetchMunicipalAssetSummary(
  organizationId: number,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    organization_id: String(organizationId),
    include_archived: "false",
    limit: "12",
  });

  return adminRequestWithTotal<MunicipalAsset[]>(
    `/assets?${params.toString()}`,
    "",
    "No se pudo cargar el inventario municipal.",
    { signal },
  );
}

export function fetchMunicipalMaintenanceSummary(
  organizationId: number,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    organization_id: String(organizationId),
    include_closed: "false",
    limit: "12",
  });

  return adminRequestWithTotal<MaintenanceOrder[]>(
    `/maintenance/orders?${params.toString()}`,
    "",
    "No se pudo cargar el mantenimiento municipal.",
    { signal },
  );
}

export function fetchGovernmentMembers(
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

export function fetchStaffPosts(organizationId: number, signal?: AbortSignal) {
  const params = new URLSearchParams({
    organization_id: String(organizationId),
    limit: "200",
  });

  return adminRequestWithTotal<StaffPost[]>(
    `/staff/posts?${params.toString()}`,
    "",
    "No se pudo cargar la plantilla municipal.",
    { signal },
  );
}

export function fetchStaffWorkers(
  organizationId: number,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    organization_id: String(organizationId),
    include_archived: "false",
    limit: "200",
  });

  return adminRequestWithTotal<StaffWorker[]>(
    `/staff/workers?${params.toString()}`,
    "",
    "No se pudo cargar el personal del ayuntamiento.",
    { signal },
  );
}

/** Detalle de una ficha: se pide al abrirla, no al listar la plantilla. */
export function fetchStaffWorkerDossier(
  workerId: number,
  options: { includeInvoices: boolean; signal?: AbortSignal },
) {
  const params = new URLSearchParams({ limit: "50" });
  const request = <T>(resource: string, fallbackError: string) =>
    adminRequest<T>(
      `/staff/workers/${workerId}/${resource}?${params.toString()}`,
      "",
      fallbackError,
      { signal: options.signal },
    );

  return Promise.all([
    request<StaffAbsence[]>("absences", "No se pudieron cargar las ausencias."),
    request<StaffReport[]>("reports", "No se pudo cargar el diario de trabajo."),
    options.includeInvoices
      ? request<StaffInvoice[]>("invoices", "No se pudieron cargar las facturas.")
      : Promise.resolve<StaffInvoice[]>([]),
  ]).then(([absences, reports, invoices]) => ({
    absences,
    reports,
    invoices,
  }));
}
