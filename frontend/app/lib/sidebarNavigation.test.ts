import { describe, expect, it } from "vitest";
import type { User } from "../components/types";
import {
  DEFAULT_SIDEBAR_SHORTCUT_IDS,
  FIXED_SIDEBAR_ITEMS,
  OPTIONAL_SIDEBAR_ITEMS,
  SIDEBAR_SHORTCUT_IDS,
  UTILITY_SIDEBAR_ITEMS,
  getActiveSidebarItemId,
  getAuthorizedFixedItems,
  getAuthorizedOptionalItems,
  getAuthorizedUtilityItems,
  getEffectiveSidebarShortcutIds,
  getVisiblePersonalSidebarItems,
  normalizeSidebarShortcutIds,
  type SidebarShortcutId,
} from "./sidebarNavigation";

function makeUser(
  permissions: string[] = [],
  overrides: Partial<User> = {},
): User {
  return {
    id: 1,
    email: "persona@example.test",
    full_name: "Persona municipal",
    is_active: true,
    is_superuser: false,
    permissions,
    ...overrides,
  };
}

describe("sidebar navigation catalog", () => {
  it("defines the fixed, utility, default and complete optional catalogs", () => {
    expect(FIXED_SIDEBAR_ITEMS.map(({ label, href }) => [label, href])).toEqual([
      ["Anacleto", "/asistente"],
      ["Inicio", "/"],
      ["Ayuntamiento", "/ayuntamiento"],
      ["Mapa", "/mapa"],
    ]);
    expect(UTILITY_SIDEBAR_ITEMS.map(({ label, href }) => [label, href])).toEqual([
      ["Mi cuenta", "/cuenta"],
    ]);
    expect(DEFAULT_SIDEBAR_SHORTCUT_IDS).toEqual([
      "ordinance_library",
      "requirements",
      "projects",
      "inventory",
      "maintenance",
      "admin",
    ]);
    expect(OPTIONAL_SIDEBAR_ITEMS).toHaveLength(19);
    expect(new Set(SIDEBAR_SHORTCUT_IDS).size).toBe(19);
    expect(
      OPTIONAL_SIDEBAR_ITEMS.find(({ id }) => id === "municipal_roadmap"),
    ).toMatchObject({
      label: "Hoja de ruta",
      href: "/ayuntamiento?tab=roadmap",
      group: "Ayuntamiento",
    });
    expect(
      OPTIONAL_SIDEBAR_ITEMS.find(({ id }) => id === "map_municipalities"),
    ).toMatchObject({
      label: "Municipios y normativa",
      href: "/mapa?view=municipalities",
    });
    expect(
      OPTIONAL_SIDEBAR_ITEMS.find(({ id }) => id === "admin_roles"),
    ).toMatchObject({ label: "Roles y permisos", href: "/admin/roles" });
  });

  it("uses dynamic defaults only for nullish preferences and keeps an explicit empty list", () => {
    expect(getEffectiveSidebarShortcutIds(makeUser())).toEqual(
      DEFAULT_SIDEBAR_SHORTCUT_IDS,
    );
    expect(
      getEffectiveSidebarShortcutIds(
        makeUser([], { sidebar_shortcut_ids: null }),
      ),
    ).toEqual(DEFAULT_SIDEBAR_SHORTCUT_IDS);
    expect(
      getEffectiveSidebarShortcutIds(
        makeUser([], { sidebar_shortcut_ids: [] }),
      ),
    ).toEqual([]);
  });

  it("normalizes unknown and duplicate values while preserving order", () => {
    expect(
      normalizeSidebarShortcutIds([
        "projects",
        "unknown",
        "projects",
        "admin_users",
        "maintenance",
      ]),
    ).toEqual(["projects", "admin_users", "maintenance"]);
  });
});

describe("sidebar permission filtering", () => {
  it("always exposes Inicio and Mi cuenta, but hides unauthorized fixed links", () => {
    const user = makeUser();

    expect(getAuthorizedFixedItems(user).map(({ id }) => id)).toEqual([
      "fixed_home",
    ]);
    expect(getAuthorizedUtilityItems(user).map(({ id }) => id)).toEqual([
      "utility_account",
    ]);
    expect(getAuthorizedOptionalItems(user)).toEqual([]);
  });

  it("shows the fixed map for either territory or municipality access", () => {
    expect(
      getAuthorizedFixedItems(makeUser(["map.view"])).map(({ id }) => id),
    ).toContain("fixed_map");
    expect(
      getAuthorizedFixedItems(makeUser(["municipalities.view"])).map(
        ({ id }) => id,
      ),
    ).toContain("fixed_map");
  });

  it("requires inventory access before exposing maintenance", () => {
    expect(
      getAuthorizedOptionalItems(makeUser(["maintenance.view"])).map(
        ({ id }) => id,
      ),
    ).not.toContain("maintenance");
    expect(
      getAuthorizedOptionalItems(
        makeUser(["assets.view", "maintenance.view"]),
      ).map(({ id }) => id),
    ).toContain("maintenance");
  });

  it("uses the same granular permissions as the admin navigation", () => {
    const groupsAdmin = makeUser(["groups.manage"]);
    expect(getAuthorizedOptionalItems(groupsAdmin).map(({ id }) => id)).toEqual([
      "admin",
      "admin_groups",
    ]);

    const memoryReviewer = makeUser([
      "assistant.use",
      "assistant.memory.review",
    ]);
    expect(
      getAuthorizedOptionalItems(memoryReviewer).map(({ id }) => id),
    ).toEqual(["admin", "admin_memory"]);
  });

  it("exposes the whole authorized catalog to a superuser", () => {
    const user = makeUser([], { is_superuser: true });

    expect(getAuthorizedFixedItems(user)).toHaveLength(4);
    expect(getAuthorizedOptionalItems(user)).toHaveLength(19);
  });

  it("keeps known dormant IDs in the saved order and only hides them at render time", () => {
    const shortcutIds: SidebarShortcutId[] = [
      "projects",
      "admin_users",
      "requirements",
    ];
    const user = makeUser(["requirements.view"], {
      sidebar_shortcut_ids: shortcutIds,
    });

    expect(getEffectiveSidebarShortcutIds(user)).toEqual(shortcutIds);
    expect(getVisiblePersonalSidebarItems(user).map(({ id }) => id)).toEqual([
      "requirements",
    ]);
  });
});

describe("query-aware active sidebar item", () => {
  it("prefers a query-specific municipal tab over the fixed municipality link", () => {
    const items = [
      FIXED_SIDEBAR_ITEMS.find(({ id }) => id === "fixed_municipality")!,
      OPTIONAL_SIDEBAR_ITEMS.find(
        ({ id }) => id === "municipal_ordinances",
      )!,
    ];

    expect(
      getActiveSidebarItemId(
        items,
        "/ayuntamiento",
        new URLSearchParams("tab=ordinances"),
      ),
    ).toBe("municipal_ordinances");
    expect(
      getActiveSidebarItemId(
        items,
        "/ayuntamiento",
        new URLSearchParams("tab=summary"),
      ),
    ).toBe("fixed_municipality");
  });

  it("prefers the municipalities map view while allowing unrelated query params", () => {
    const items = [
      FIXED_SIDEBAR_ITEMS.find(({ id }) => id === "fixed_map")!,
      OPTIONAL_SIDEBAR_ITEMS.find(({ id }) => id === "map_municipalities")!,
    ];

    expect(
      getActiveSidebarItemId(
        items,
        "/mapa",
        "view=municipalities&organization_id=7",
      ),
    ).toBe("map_municipalities");
    expect(getActiveSidebarItemId(items, "/mapa", "view=territory")).toBe(
      "fixed_map",
    );
  });

  it("prefers the most specific rendered pathname and treats root as exact", () => {
    const admin = OPTIONAL_SIDEBAR_ITEMS.find(({ id }) => id === "admin")!;
    const users = OPTIONAL_SIDEBAR_ITEMS.find(
      ({ id }) => id === "admin_users",
    )!;
    const home = FIXED_SIDEBAR_ITEMS.find(({ id }) => id === "fixed_home")!;

    expect(
      getActiveSidebarItemId([admin, users], "/admin/usuarios", null),
    ).toBe("admin_users");
    expect(getActiveSidebarItemId([admin], "/admin/usuarios", null)).toBe(
      "admin",
    );
    expect(getActiveSidebarItemId([home, admin], "/admin", null)).toBe(
      "admin",
    );
  });
});
