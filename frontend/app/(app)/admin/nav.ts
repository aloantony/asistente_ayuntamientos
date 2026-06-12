import { userHasPermission, type User } from "../../components/types";
import {
  MUNICIPALITY_PERMISSIONS,
  ORDINANCE_PERMISSIONS,
} from "../../lib/permissions";

export function canUseMunicipalitiesSection(user: User) {
  return MUNICIPALITY_PERMISSIONS.some((permissionCode) =>
    userHasPermission(user, permissionCode),
  );
}

export function canUseOrdinancesSection(user: User) {
  return ORDINANCE_PERMISSIONS.some((permissionCode) =>
    userHasPermission(user, permissionCode),
  );
}

// Permisos que exige el backend para LISTAR (GET de colección): un usuario
// solo-crear/editar/archivar accede a la sección pero no debe disparar una
// petición de listado condenada al 403.
export function canListMunicipalities(user: User) {
  return (
    userHasPermission(user, "municipalities.view") ||
    userHasPermission(user, "municipalities.manage")
  );
}

export function canListOrdinances(user: User) {
  return (
    userHasPermission(user, "ordinances.view") ||
    userHasPermission(user, "ordinances.manage")
  );
}

export type AdminNavItem = {
  href: string;
  label: string;
};

// El orden define la prioridad de la redirección de /admin.
export function getAdminNavItems(user: User): AdminNavItem[] {
  return [
    ...(userHasPermission(user, "users.manage")
      ? [{ href: "/admin/usuarios", label: "Usuarios" }]
      : []),
    ...(userHasPermission(user, "groups.manage")
      ? [{ href: "/admin/grupos", label: "Grupos" }]
      : []),
    ...(userHasPermission(user, "organizations.manage")
      ? [{ href: "/admin/organizaciones", label: "Organizaciones" }]
      : []),
    ...(userHasPermission(user, "roles.manage")
      ? [{ href: "/admin/roles", label: "Roles y permisos" }]
      : []),
    ...(canUseMunicipalitiesSection(user)
      ? [{ href: "/admin/municipios", label: "Municipios" }]
      : []),
    ...(canUseOrdinancesSection(user)
      ? [{ href: "/admin/ordenanzas", label: "Ordenanzas" }]
      : []),
  ];
}
