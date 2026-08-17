import type {
  MunicipalContract,
  MunicipalGrant,
  MunicipalLicence,
  MunicipalNotice,
  OfficeHour,
} from "../components/types";
import { adminRequestWithTotal } from "./api";

// Administración y comunicación municipal (ADR-040). La pantalla del
// ayuntamiento enseña un resumen; la gestión completa vive en sus propias
// pantallas cuando existan.

function withOrganization(organizationId: number, limit = 50) {
  return new URLSearchParams({
    organization_id: String(organizationId),
    limit: String(limit),
  });
}

export function fetchOfficeHours(organizationId: number, signal?: AbortSignal) {
  return adminRequestWithTotal<OfficeHour[]>(
    `/administration/office-hours?${withOrganization(organizationId).toString()}`,
    "",
    "No se pudo cargar el horario de atención.",
    { signal },
  );
}

export function fetchLicences(organizationId: number, signal?: AbortSignal) {
  return adminRequestWithTotal<MunicipalLicence[]>(
    `/administration/licences?${withOrganization(organizationId, 12).toString()}`,
    "",
    "No se pudieron cargar las licencias.",
    { signal },
  );
}

export function fetchContracts(organizationId: number, signal?: AbortSignal) {
  return adminRequestWithTotal<MunicipalContract[]>(
    `/administration/contracts?${withOrganization(organizationId, 12).toString()}`,
    "",
    "No se pudieron cargar los contratos.",
    { signal },
  );
}

export function fetchGrants(organizationId: number, signal?: AbortSignal) {
  return adminRequestWithTotal<MunicipalGrant[]>(
    `/administration/grants?${withOrganization(organizationId, 12).toString()}`,
    "",
    "No se pudieron cargar las subvenciones.",
    { signal },
  );
}

export function fetchNotices(organizationId: number, signal?: AbortSignal) {
  return adminRequestWithTotal<MunicipalNotice[]>(
    `/communications/notices?${withOrganization(organizationId, 12).toString()}`,
    "",
    "No se pudieron cargar los bandos.",
    { signal },
  );
}
