import {
  userHasPermission,
  type SidebarShortcutId,
  type User,
} from "../components/types";
import {
  ADMIN_MANAGEMENT_PERMISSIONS,
  canUseMemoryReview,
  canUseProductReview,
  canViewMunicipalHub,
  canViewOrdinanceLibrary,
  PROJECT_PERMISSIONS,
} from "./permissions";

export type { SidebarShortcutId } from "../components/types";

export type SidebarFixedItemId =
  | "fixed_assistant"
  | "fixed_home"
  | "fixed_municipality"
  | "fixed_map";

export type SidebarUtilityItemId = "utility_account";

export type SidebarItemId =
  | SidebarFixedItemId
  | SidebarShortcutId
  | SidebarUtilityItemId;

export type SidebarNavIcon =
  | "account"
  | "admin"
  | "iconcejo"
  | "home"
  | "inventory"
  | "maintenance"
  | "map"
  | "needs"
  | "ordinances"
  | "projects"
  | "townhall";

export type SidebarNavGroup =
  | "Principal"
  | "Trabajo"
  | "Territorio"
  | "Ayuntamiento"
  | "Mapa"
  | "Administración"
  | "Cuenta";

export type SidebarNavTier = "fixed" | "optional" | "utility";

export type SidebarNavItem = {
  id: SidebarItemId;
  label: string;
  href: string;
  icon: SidebarNavIcon;
  group: SidebarNavGroup;
  tier: SidebarNavTier;
  prominent?: boolean;
};

type SidebarNavDefinition = SidebarNavItem & {
  isAuthorized: (user: User) => boolean;
};

const REQUIREMENT_PERMISSIONS = [
  "requirements.view",
  "requirements.create",
  "requirements.edit",
  "requirements.review",
  "requirements.archive",
  "requirements.manage",
] as const;

const ASSET_VIEW_PERMISSIONS = ["assets.view", "assets.manage"] as const;
const MAINTENANCE_VIEW_PERMISSIONS = [
  "maintenance.view",
  "maintenance.manage",
] as const;
const MAP_VIEW_PERMISSIONS = ["map.view", "map.manage"] as const;
const MUNICIPALITY_ADMIN_PERMISSIONS = [
  "municipalities.create",
  "municipalities.edit",
  "municipalities.archive",
  "municipalities.manage",
] as const;
const ORDINANCE_ADMIN_PERMISSIONS = [
  "ordinances.create",
  "ordinances.edit",
  "ordinances.archive",
  "ordinances.import",
  "ordinances.review",
  "ordinances.manage",
] as const;

function hasAnyPermission(user: User, permissions: readonly string[]) {
  return permissions.some((permission) =>
    userHasPermission(user, permission),
  );
}

function canViewAssets(user: User) {
  return hasAnyPermission(user, ASSET_VIEW_PERMISSIONS);
}

function canViewMaintenance(user: User) {
  return (
    canViewAssets(user) &&
    hasAnyPermission(user, MAINTENANCE_VIEW_PERMISSIONS)
  );
}

function canViewMap(user: User) {
  return hasAnyPermission(user, MAP_VIEW_PERMISSIONS);
}

function canViewMapSection(user: User) {
  return canViewMap(user) || canViewMunicipalHub(user);
}

function canViewRequirements(user: User) {
  return hasAnyPermission(user, REQUIREMENT_PERMISSIONS);
}

function canViewProjects(user: User) {
  return hasAnyPermission(user, PROJECT_PERMISSIONS);
}

function canViewAdmin(user: User) {
  return (
    hasAnyPermission(user, ADMIN_MANAGEMENT_PERMISSIONS) ||
    canUseProductReview(user) ||
    canUseMemoryReview(user)
  );
}

function canUseMunicipalitiesAdmin(user: User) {
  return hasAnyPermission(user, MUNICIPALITY_ADMIN_PERMISSIONS);
}

function canUseOrdinancesAdmin(user: User) {
  return hasAnyPermission(user, ORDINANCE_ADMIN_PERMISSIONS);
}

const FIXED_DEFINITIONS = [
  {
    id: "fixed_assistant",
    label: "iConcejo",
    href: "/asistente",
    icon: "iconcejo",
    group: "Principal",
    tier: "fixed",
    prominent: true,
    isAuthorized: (user) => userHasPermission(user, "assistant.use"),
  },
  {
    id: "fixed_home",
    label: "Inicio",
    href: "/",
    icon: "home",
    group: "Principal",
    tier: "fixed",
    isAuthorized: () => true,
  },
  {
    id: "fixed_municipality",
    label: "Ayuntamiento",
    href: "/ayuntamiento",
    icon: "townhall",
    group: "Principal",
    tier: "fixed",
    isAuthorized: canViewMunicipalHub,
  },
  {
    id: "fixed_map",
    label: "Mapa",
    href: "/mapa",
    icon: "map",
    group: "Principal",
    tier: "fixed",
    isAuthorized: canViewMapSection,
  },
] satisfies readonly SidebarNavDefinition[];

const OPTIONAL_DEFINITIONS = [
  {
    id: "ordinance_library",
    label: "Ordenanzas",
    href: "/ordenanzas",
    icon: "ordinances",
    group: "Trabajo",
    tier: "optional",
    isAuthorized: canViewOrdinanceLibrary,
  },
  {
    id: "requirements",
    label: "Necesidades",
    href: "/requisitos",
    icon: "needs",
    group: "Trabajo",
    tier: "optional",
    isAuthorized: canViewRequirements,
  },
  {
    id: "projects",
    label: "Proyectos",
    href: "/proyectos",
    icon: "projects",
    group: "Trabajo",
    tier: "optional",
    isAuthorized: canViewProjects,
  },
  {
    id: "inventory",
    label: "Inventario",
    href: "/inventario",
    icon: "inventory",
    group: "Territorio",
    tier: "optional",
    isAuthorized: canViewAssets,
  },
  {
    id: "maintenance",
    label: "Mantenimiento",
    href: "/mantenimiento",
    icon: "maintenance",
    group: "Territorio",
    tier: "optional",
    isAuthorized: canViewMaintenance,
  },
  {
    id: "admin",
    label: "Administración",
    href: "/admin",
    icon: "admin",
    group: "Administración",
    tier: "optional",
    isAuthorized: canViewAdmin,
  },
  {
    id: "municipal_ordinances",
    label: "Normativa",
    href: "/ordenanzas",
    icon: "ordinances",
    group: "Ayuntamiento",
    tier: "optional",
    isAuthorized: (user) =>
      canViewMunicipalHub(user) && canViewOrdinanceLibrary(user),
  },
  {
    id: "municipal_facilities",
    label: "Instalaciones",
    href: "/ayuntamiento?tab=administration",
    icon: "inventory",
    group: "Ayuntamiento",
    tier: "optional",
    isAuthorized: (user) => canViewMunicipalHub(user) && canViewAssets(user),
  },
  {
    id: "municipal_people",
    label: "Personal",
    href: "/ayuntamiento?tab=people",
    icon: "account",
    group: "Ayuntamiento",
    tier: "optional",
    isAuthorized: canViewMunicipalHub,
  },
  {
    id: "municipal_roadmap",
    label: "Hoja de ruta",
    href: "/hoja-de-ruta",
    icon: "townhall",
    group: "Ayuntamiento",
    tier: "optional",
    isAuthorized: canViewMunicipalHub,
  },
  {
    id: "map_municipalities",
    label: "Municipios y normativa",
    href: "/mapa?view=municipalities",
    icon: "map",
    group: "Mapa",
    tier: "optional",
    isAuthorized: canViewMunicipalHub,
  },
  {
    id: "admin_product",
    label: "Producto",
    href: "/admin/producto",
    icon: "admin",
    group: "Administración",
    tier: "optional",
    isAuthorized: canUseProductReview,
  },
  {
    id: "admin_memory",
    label: "Memoria",
    href: "/admin/memoria",
    icon: "admin",
    group: "Administración",
    tier: "optional",
    isAuthorized: canUseMemoryReview,
  },
  {
    id: "admin_users",
    label: "Usuarios",
    href: "/admin/usuarios",
    icon: "admin",
    group: "Administración",
    tier: "optional",
    isAuthorized: (user) => userHasPermission(user, "users.manage"),
  },
  {
    id: "admin_groups",
    label: "Grupos",
    href: "/admin/grupos",
    icon: "admin",
    group: "Administración",
    tier: "optional",
    isAuthorized: (user) => userHasPermission(user, "groups.manage"),
  },
  {
    id: "admin_organizations",
    label: "Organizaciones",
    href: "/admin/organizaciones",
    icon: "admin",
    group: "Administración",
    tier: "optional",
    isAuthorized: (user) => userHasPermission(user, "organizations.manage"),
  },
  {
    id: "admin_roles",
    label: "Roles y permisos",
    href: "/admin/roles",
    icon: "admin",
    group: "Administración",
    tier: "optional",
    isAuthorized: (user) => userHasPermission(user, "roles.manage"),
  },
  {
    id: "admin_municipalities",
    label: "Municipios",
    href: "/admin/municipios",
    icon: "admin",
    group: "Administración",
    tier: "optional",
    isAuthorized: canUseMunicipalitiesAdmin,
  },
  {
    id: "admin_ordinances",
    label: "Ordenanzas",
    href: "/admin/ordenanzas",
    icon: "admin",
    group: "Administración",
    tier: "optional",
    isAuthorized: canUseOrdinancesAdmin,
  },
] satisfies readonly SidebarNavDefinition[];

const UTILITY_DEFINITIONS: readonly SidebarNavDefinition[] = [
  {
    id: "utility_account",
    label: "Mi cuenta",
    href: "/cuenta",
    icon: "account",
    group: "Cuenta",
    tier: "utility",
    isAuthorized: () => true,
  },
];

function toNavItem(definition: SidebarNavDefinition): SidebarNavItem {
  const { isAuthorized, ...item } = definition;
  void isAuthorized;
  return item;
}

export const FIXED_SIDEBAR_ITEMS: readonly SidebarNavItem[] =
  FIXED_DEFINITIONS.map(toNavItem);

export const OPTIONAL_SIDEBAR_ITEMS: readonly SidebarNavItem[] =
  OPTIONAL_DEFINITIONS.map(toNavItem);

export const UTILITY_SIDEBAR_ITEMS: readonly SidebarNavItem[] =
  UTILITY_DEFINITIONS.map(toNavItem);

export const DEFAULT_SIDEBAR_SHORTCUT_IDS: readonly SidebarShortcutId[] = [
  "ordinance_library",
  "requirements",
  "projects",
  "inventory",
  "maintenance",
  "admin",
];

export const SIDEBAR_SHORTCUT_IDS: readonly SidebarShortcutId[] =
  OPTIONAL_DEFINITIONS.map(({ id }) => id);

const shortcutIdSet = new Set<string>(SIDEBAR_SHORTCUT_IDS);
const optionalDefinitionById = new Map(
  OPTIONAL_DEFINITIONS.map((definition) => [definition.id, definition]),
);

export function isSidebarShortcutId(value: string): value is SidebarShortcutId {
  return shortcutIdSet.has(value);
}

export function normalizeSidebarShortcutIds(
  shortcutIds: readonly string[],
): SidebarShortcutId[] {
  const seen = new Set<SidebarShortcutId>();
  const normalized: SidebarShortcutId[] = [];

  for (const shortcutId of shortcutIds) {
    if (!isSidebarShortcutId(shortcutId) || seen.has(shortcutId)) {
      continue;
    }
    seen.add(shortcutId);
    normalized.push(shortcutId);
  }

  return normalized;
}

export function getAuthorizedFixedItems(user: User): SidebarNavItem[] {
  return FIXED_DEFINITIONS.filter(({ isAuthorized }) => isAuthorized(user)).map(
    toNavItem,
  );
}

export function getAuthorizedOptionalItems(user: User): SidebarNavItem[] {
  return OPTIONAL_DEFINITIONS.filter(({ isAuthorized }) =>
    isAuthorized(user),
  ).map(toNavItem);
}

export function getAuthorizedUtilityItems(user: User): SidebarNavItem[] {
  return UTILITY_DEFINITIONS.filter(({ isAuthorized }) =>
    isAuthorized(user),
  ).map(toNavItem);
}

export function getEffectiveSidebarShortcutIds(
  user: Pick<User, "sidebar_shortcut_ids">,
): SidebarShortcutId[] {
  return normalizeSidebarShortcutIds(
    user.sidebar_shortcut_ids ?? DEFAULT_SIDEBAR_SHORTCUT_IDS,
  );
}

export function getVisiblePersonalSidebarItems(user: User): SidebarNavItem[] {
  return getEffectiveSidebarShortcutIds(user).flatMap((shortcutId) => {
    const definition = optionalDefinitionById.get(shortcutId);
    if (!definition || !definition.isAuthorized(user)) {
      return [];
    }
    return [toNavItem(definition)];
  });
}

type SearchParamsLike = Pick<URLSearchParams, "getAll">;

function toSearchParams(searchParams: SearchParamsLike | string | null) {
  if (typeof searchParams === "string") {
    return new URLSearchParams(searchParams);
  }
  return searchParams ?? new URLSearchParams();
}

function pathnameMatches(currentPathname: string, itemPathname: string) {
  if (itemPathname === "/") {
    return currentPathname === "/";
  }
  return (
    currentPathname === itemPathname ||
    currentPathname.startsWith(`${itemPathname}/`)
  );
}

/**
 * Returns the single active item among the links that are actually rendered.
 * A subroute wins over its parent and a query-aware shortcut wins over the
 * query-agnostic link for the same pathname.
 */
export function getActiveSidebarItemId(
  items: readonly SidebarNavItem[],
  pathname: string,
  searchParams: SearchParamsLike | string | null = null,
): SidebarItemId | null {
  const currentSearchParams = toSearchParams(searchParams);
  let active: { id: SidebarItemId; score: number } | null = null;

  for (const item of items) {
    const itemUrl = new URL(item.href, "https://sidebar.invalid");
    if (!pathnameMatches(pathname, itemUrl.pathname)) {
      continue;
    }

    const requestedQueryEntries = Array.from(itemUrl.searchParams.entries());
    const queryMatches = requestedQueryEntries.every(([name, value]) =>
      currentSearchParams.getAll(name).includes(value),
    );
    if (!queryMatches) {
      continue;
    }

    const score = itemUrl.pathname.length * 100 + requestedQueryEntries.length;
    if (!active || score > active.score) {
      active = { id: item.id, score };
    }
  }

  return active?.id ?? null;
}
