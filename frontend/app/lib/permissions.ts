import { userHasPermission, type User } from "../components/types";

// Códigos y predicados compartidos por el sidebar (session.tsx) y la
// subnavegación de admin (nav.ts): una única fuente evita que el acceso visual
// diverja del contrato que aplican las páginas.

export const MUNICIPALITY_PERMISSIONS = [
  "municipalities.view",
  "municipalities.create",
  "municipalities.edit",
  "municipalities.archive",
  "municipalities.manage",
];

export const ORDINANCE_PERMISSIONS = [
  "ordinances.view",
  "ordinances.create",
  "ordinances.edit",
  "ordinances.archive",
  "ordinances.import",
  "ordinances.review",
  "ordinances.compare",
  "ordinances.manage",
];

export const TOWN_HALL_PERMISSIONS = [
  "town_hall.view",
  "town_hall.edit",
  "town_hall.manage",
];

export const TOWN_HALL_EDIT_PERMISSIONS = [
  "town_hall.edit",
  "town_hall.manage",
];

export const PROJECT_PERMISSIONS = [
  "projects.view_all",
  "projects.create",
  "projects.edit",
  "projects.archive",
  "projects.manage_members",
  "projects.manage",
];

export const ADMIN_MANAGEMENT_PERMISSIONS = [
  "users.manage",
  "groups.manage",
  "organizations.manage",
  "roles.manage",
  "municipalities.create",
  "municipalities.edit",
  "municipalities.archive",
  "municipalities.manage",
  "ordinances.create",
  "ordinances.edit",
  "ordinances.archive",
  "ordinances.import",
  "ordinances.review",
  "ordinances.manage",
];

// Permisos de las secciones administrativas tradicionales. Producto y memoria
// usan predicados compuestos porque no se pueden expresar como una lista OR.
export const ADMIN_PANEL_PERMISSIONS = ADMIN_MANAGEMENT_PERMISSIONS;

export function canUseProductReview(user: User) {
  return user.is_superuser;
}

export function canUseMemoryReview(user: User) {
  return (
    userHasPermission(user, "assistant.use") &&
    userHasPermission(user, "assistant.memory.review")
  );
}
