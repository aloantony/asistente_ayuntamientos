import type {
  Group,
  Municipality,
  Organization,
  Project,
  User,
} from "../components/types";
import { adminRequest, adminRequestWithTotal } from "./api";

// Small standalone loaders for pages that need shared reference data
// without instantiating a whole domain controller. Auth travels in the
// httpOnly cookie, so the token argument is always empty.

export function fetchOrganizations() {
  // GET /organizations devuelve organizaciones completas; los consumidores
  // que solo necesitan el resumen siguen siendo compatibles.
  return adminRequest<Organization[]>(
    "/organizations",
    "",
    "No se pudo cargar la lista de organizaciones.",
  );
}

export function fetchProjects() {
  return adminRequest<Project[]>(
    "/projects",
    "",
    "No se pudieron cargar los proyectos.",
  );
}

export function fetchAdminUsers() {
  return adminRequest<User[]>(
    "/admin/users",
    "",
    "No se pudo cargar la lista de usuarios.",
  );
}

export function fetchAdminGroups() {
  return adminRequest<Group[]>(
    "/admin/groups",
    "",
    "No se pudo cargar la lista de grupos.",
  );
}

export function fetchMunicipalityOptions() {
  // Opciones para los selectores de municipio (organizaciones y ordenanzas).
  return adminRequest<Municipality[]>(
    "/municipalities?limit=200",
    "",
    "No se pudo cargar la lista de municipios.",
  );
}

export function fetchMunicipality(
  municipalityId: number,
  signal?: AbortSignal,
) {
  return adminRequest<Municipality>(
    `/municipalities/${municipalityId}`,
    "",
    "No se pudo cargar la información del municipio.",
    { signal },
  );
}

// Totales para las tarjetas de métricas del panel de inicio: piden una sola
// fila (limit=1) y leen el conteo real de la cabecera X-Total-Count, sin
// traerse la lista entera. Cada llamada exige el permiso de su recurso, así
// que el dashboard solo invoca las que el usuario puede ver.
async function fetchTotal(path: string, fallbackError: string) {
  const { total } = await adminRequestWithTotal<unknown[]>(
    path,
    "",
    fallbackError,
  );
  return total;
}

export function fetchRequirementsTotal() {
  return fetchTotal(
    "/requirements?limit=1",
    "No se pudieron contar las necesidades.",
  );
}

export function fetchMunicipalitiesTotal() {
  return fetchTotal(
    "/municipalities?limit=1",
    "No se pudo contar la lista de municipios.",
  );
}

export function fetchOrdinancesTotal() {
  return fetchTotal(
    "/ordinances?limit=1",
    "No se pudo contar la lista de ordenanzas.",
  );
}
