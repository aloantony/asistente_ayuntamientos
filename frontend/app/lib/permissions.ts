// Códigos de permiso compartidos por el sidebar (session.tsx) y la
// subnavegación de admin (nav.ts): única fuente para que el predicado del
// panel y el de sus secciones no puedan divergir al añadir permisos nuevos.

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

// Cualquier permiso que abre alguna sección de /admin. Los permisos de solo
// consulta (por ejemplo ordinances.view/compare del alcalde piloto) no deben
// mostrar el área de administración.
export const ADMIN_PANEL_PERMISSIONS = ADMIN_MANAGEMENT_PERMISSIONS;
