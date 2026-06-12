import type {
  Group,
  Municipality,
  Organization,
  Project,
  User,
} from "../components/types";
import { adminRequest } from "./api";

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
