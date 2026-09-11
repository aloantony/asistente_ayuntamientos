"use client";

import { LayoutGrid } from "lucide-react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import {
  useCallback,
  useRef,
  useState,
  type RefObject,
} from "react";
import { deleteSidebarShortcuts, putSidebarShortcuts } from "../lib/api";
import {
  DEFAULT_SIDEBAR_SHORTCUT_IDS,
  getActiveSidebarItemId,
  getAuthorizedFixedItems,
  getAuthorizedOptionalItems,
  getAuthorizedUtilityItems,
  getEffectiveSidebarShortcutIds,
  getVisiblePersonalSidebarItems,
  type SidebarNavItem,
  type SidebarShortcutId,
} from "../lib/sidebarNavigation";
import { useSession } from "../lib/session";
import type { User } from "./types";
import { SidebarCustomizeDialog } from "./SidebarCustomizeDialog";
import { SidebarNavIcon } from "./SidebarNavIcon";

type SidebarNavigationProps = {
  isCollapsed: boolean;
  isMenuOpen: boolean;
  navigationRef: RefObject<HTMLElement | null>;
  onNavigate: () => void;
  requirementsTotal: number | null;
  user: User;
};

// Estas rutas ya tienen una entrada clara dentro de Ayuntamiento. Repetirlas
// en la cabecera ocupa el ancho disponible y obliga a desplazar una navegación
// que debe poder leerse de un vistazo. Se conservan en el catálogo completo:
// esto sólo decide qué accesos rápidos merecen sitio en la cabecera.
const TOWN_HALL_REDUNDANT_HEADER_ITEMS = new Set<SidebarNavItem["id"]>([
  "fixed_map",
  "ordinance_library",
  "inventory",
  "maintenance",
  "municipal_ordinances",
  "municipal_facilities",
  "municipal_people",
  "municipal_roadmap",
]);

export function SidebarNavigation({
  isCollapsed,
  isMenuOpen,
  navigationRef,
  onNavigate,
  requirementsTotal,
  user,
}: SidebarNavigationProps) {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { handleRequestError, setUser } = useSession();
  const catalogTriggerRef = useRef<HTMLButtonElement>(null);
  const [dialogMode, setDialogMode] = useState<"browse" | "edit">("browse");
  const [isDialogOpen, setIsDialogOpen] = useState(false);

  const fixedItems = getAuthorizedFixedItems(user);
  const optionalItems = getAuthorizedOptionalItems(user);
  const personalItems = getVisiblePersonalSidebarItems(user);
  const utilityItems = getAuthorizedUtilityItems(user);
  const headerFixedItems = fixedItems.filter(
    (item) => !TOWN_HALL_REDUNDANT_HEADER_ITEMS.has(item.id),
  );
  const headerPersonalItems = personalItems.filter(
    (item) => !TOWN_HALL_REDUNDANT_HEADER_ITEMS.has(item.id),
  );
  const effectiveShortcutIds = getEffectiveSidebarShortcutIds(user);
  const renderedItems = [
    ...headerFixedItems,
    ...headerPersonalItems,
    ...utilityItems,
  ];
  const activeItemId = getActiveSidebarItemId(
    renderedItems,
    pathname,
    searchParams,
  );

  const closeDialog = useCallback(() => {
    setIsDialogOpen(false);
    window.requestAnimationFrame(() => catalogTriggerRef.current?.focus());
  }, []);

  function openCatalog(mode: "browse" | "edit" = "browse") {
    setDialogMode(mode);
    setIsDialogOpen(true);
  }

  async function saveShortcuts(
    shortcutIds: SidebarShortcutId[],
    resetToDefaults: boolean,
  ) {
    try {
      const response = resetToDefaults
        ? await deleteSidebarShortcuts()
        : await putSidebarShortcuts(shortcutIds);
      setUser((currentUser) =>
        currentUser
          ? {
              ...currentUser,
              sidebar_shortcut_ids: response.shortcut_ids,
            }
          : currentUser,
      );
    } catch (requestError) {
      let message = "";
      handleRequestError(
        requestError,
        (nextMessage) => {
          message = nextMessage;
        },
        "No se pudieron guardar tus accesos. Inténtalo de nuevo.",
      );
      if (message) {
        throw new Error(message);
      }
      throw requestError;
    }
  }

  function renderLink(item: SidebarNavItem) {
    const isActive = item.id === activeItemId;
    const isAssistant = item.id === "fixed_assistant";
    const badge = item.id === "requirements" ? requirementsTotal : null;

    return (
      <Link
        aria-current={isActive ? "page" : undefined}
        className={`app-nav-link${isActive ? " active" : ""}${
          item.prominent ? " app-nav-link--primary" : ""
        }`}
        href={item.href}
        key={item.id}
        onClick={onNavigate}
        title={isCollapsed ? item.label : undefined}
      >
        <SidebarNavIcon name={item.icon} />
        <span className="app-nav-label">{item.label}</span>
        {isAssistant ? <span className="app-nav-beta">BETA</span> : null}
        {typeof badge === "number" ? (
          <span className="app-nav-badge">{badge}</span>
        ) : null}
      </Link>
    );
  }

  return (
    <>
      <nav
        aria-label="Navegación principal"
        className={isMenuOpen ? "app-nav app-nav--open" : "app-nav"}
        id="app-primary-navigation"
        ref={navigationRef}
      >
        <div className="app-nav-group">
          <span className="app-nav-section">Principal</span>
          {headerFixedItems.map(renderLink)}
        </div>

        <div className="app-nav-group">
          <span className="app-nav-section">Mis accesos</span>
          {headerPersonalItems.length > 0
            ? headerPersonalItems.map(renderLink)
            : null}
          {personalItems.length === 0 ? (
            <p className="app-nav-empty">
              Añade aquí las secciones que usas a diario.
            </p>
          ) : null}
          <button
            className="app-nav-link app-nav-catalog-button"
            onClick={() => openCatalog("browse")}
            ref={catalogTriggerRef}
            title={isCollapsed ? "Todas las secciones" : undefined}
            type="button"
          >
            <LayoutGrid aria-hidden="true" size={17} />
            <span className="app-nav-label">Todas las secciones</span>
          </button>
        </div>

        {utilityItems.length > 0 ? (
          <div className="app-nav-group app-nav-utility">
            <span className="app-nav-section">Cuenta</span>
            {utilityItems.map(renderLink)}
          </div>
        ) : null}
      </nav>

      {isDialogOpen ? (
        <SidebarCustomizeDialog
          defaultIds={DEFAULT_SIDEBAR_SHORTCUT_IDS}
          fixedItems={fixedItems}
          initialMode={dialogMode}
          onClose={closeDialog}
          onNavigate={onNavigate}
          onSave={saveShortcuts}
          optionalItems={optionalItems}
          selectedIds={effectiveShortcutIds}
          utilityItems={utilityItems}
        />
      ) : null}
    </>
  );
}
