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
  "ordinances.manage",
];

// Cualquier permiso que abre alguna sección de /admin: las mismas listas con
// las que getAdminNavItems decide qué pestañas se muestran.
export const ADMIN_PANEL_PERMISSIONS = [
  "users.manage",
  "groups.manage",
  "organizations.manage",
  "roles.manage",
  ...MUNICIPALITY_PERMISSIONS,
  ...ORDINANCE_PERMISSIONS,
];
