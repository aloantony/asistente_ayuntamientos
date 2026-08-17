"use client";

import {
  ArrowDown,
  ArrowUp,
  Check,
  GripVertical,
  LockKeyhole,
  Plus,
  RotateCcw,
  Settings2,
  X,
} from "lucide-react";
import Link from "next/link";
import {
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { getErrorMessage } from "../lib/api";
import type {
  SidebarNavItem,
  SidebarShortcutId,
} from "../lib/sidebarNavigation";
import {
  SidebarNavIcon,
  type SidebarNavIconName,
} from "./SidebarNavIcon";

type SidebarCustomizeDialogProps = {
  defaultIds: readonly SidebarShortcutId[];
  fixedItems: SidebarNavItem[];
  initialMode?: "browse" | "edit";
  onClose: () => void;
  onNavigate: () => void;
  onSave: (
    shortcutIds: SidebarShortcutId[],
    resetToDefaults: boolean,
  ) => Promise<void>;
  optionalItems: SidebarNavItem[];
  selectedIds: SidebarShortcutId[];
  utilityItems: SidebarNavItem[];
};

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
const BACKGROUND_SELECTOR = [
  ".app-brand",
  ".menu-toggle",
  ".menu-drawer-scrim",
  ".app-nav",
  ".app-session",
  ".app-main",
].join(", ");

function groupItems(items: SidebarNavItem[]) {
  const groups = new Map<string, SidebarNavItem[]>();

  for (const item of items) {
    const group = item.group || "Otras secciones";
    groups.set(group, [...(groups.get(group) ?? []), item]);
  }

  return Array.from(groups.entries());
}

export function SidebarCustomizeDialog({
  defaultIds,
  fixedItems,
  initialMode = "browse",
  onClose,
  onNavigate,
  onSave,
  optionalItems,
  selectedIds,
  utilityItems,
}: SidebarCustomizeDialogProps) {
  const dialogRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const [mode, setMode] = useState<"browse" | "edit">(initialMode);
  const [draftIds, setDraftIds] = useState<SidebarShortcutId[]>(selectedIds);
  const [resetToDefaults, setResetToDefaults] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState("");
  const [announcement, setAnnouncement] = useState("");
  const [draggingId, setDraggingId] = useState<SidebarShortcutId | null>(null);

  const optionalById = new Map(
    optionalItems.map((item) => [item.id as SidebarShortcutId, item]),
  );
  const visibleSelectedItems = draftIds
    .map((shortcutId) => optionalById.get(shortcutId))
    .filter((item): item is SidebarNavItem => Boolean(item));
  const selectedIdSet = new Set(draftIds);
  const availableItems = optionalItems.filter(
    (item) => !selectedIdSet.has(item.id as SidebarShortcutId),
  );
  const optionalGroups = groupItems(optionalItems);
  const availableGroups = groupItems(availableItems);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    const backgroundElements = Array.from(
      document.querySelectorAll<HTMLElement>(BACKGROUND_SELECTOR),
    );
    const previouslyInertElements = new Set(
      backgroundElements.filter((element) => element.hasAttribute("inert")),
    );
    backgroundElements.forEach((element) => element.setAttribute("inert", ""));
    document.body.style.overflow = "hidden";
    const focusFrame = window.requestAnimationFrame(() => {
      closeButtonRef.current?.focus();
    });

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }

      if (event.key !== "Tab" || !dialogRef.current) {
        return;
      }

      const focusableElements = Array.from(
        dialogRef.current.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
      ).filter((element) => !element.hasAttribute("disabled"));
      const firstElement = focusableElements[0];
      const lastElement = focusableElements.at(-1);

      if (!firstElement || !lastElement) {
        event.preventDefault();
        dialogRef.current.focus();
      } else if (event.shiftKey && document.activeElement === firstElement) {
        event.preventDefault();
        lastElement.focus();
      } else if (!event.shiftKey && document.activeElement === lastElement) {
        event.preventDefault();
        firstElement.focus();
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = previousOverflow;
      backgroundElements.forEach((element) => {
        if (!previouslyInertElements.has(element)) {
          element.removeAttribute("inert");
        }
      });
    };
  }, [onClose]);

  function enterEditMode() {
    setDraftIds(selectedIds);
    setResetToDefaults(false);
    setError("");
    setMode("edit");
  }

  function cancelEditing() {
    setDraftIds(selectedIds);
    setResetToDefaults(false);
    setError("");
    setAnnouncement("Cambios descartados.");
    setMode("browse");
  }

  function addShortcut(shortcutId: SidebarShortcutId) {
    setDraftIds((currentIds) =>
      currentIds.includes(shortcutId)
        ? currentIds
        : [...currentIds, shortcutId],
    );
    setResetToDefaults(false);
    setError("");
    setAnnouncement(`${optionalById.get(shortcutId)?.label ?? "El acceso"} añadido.`);
  }

  function removeShortcut(shortcutId: SidebarShortcutId) {
    const label = optionalById.get(shortcutId)?.label ?? "El acceso";
    setDraftIds((currentIds) =>
      currentIds.filter((currentId) => currentId !== shortcutId),
    );
    setResetToDefaults(false);
    setError("");
    setAnnouncement(`${label} eliminado de Mis accesos.`);
  }

  function swapVisibleShortcuts(
    sourceId: SidebarShortcutId,
    targetId: SidebarShortcutId,
  ) {
    if (sourceId === targetId) {
      return;
    }

    setDraftIds((currentIds) => {
      const sourceIndex = currentIds.indexOf(sourceId);
      const targetIndex = currentIds.indexOf(targetId);
      if (sourceIndex < 0 || targetIndex < 0) {
        return currentIds;
      }

      const nextIds = [...currentIds];
      [nextIds[sourceIndex], nextIds[targetIndex]] = [
        nextIds[targetIndex],
        nextIds[sourceIndex],
      ];
      return nextIds;
    });
    setResetToDefaults(false);
    setError("");
  }

  function moveShortcut(shortcutId: SidebarShortcutId, offset: -1 | 1) {
    const visibleIndex = visibleSelectedItems.findIndex(
      (item) => item.id === shortcutId,
    );
    const target = visibleSelectedItems[visibleIndex + offset];
    if (!target) {
      return;
    }

    swapVisibleShortcuts(shortcutId, target.id as SidebarShortcutId);
    const movedItem = optionalById.get(shortcutId);
    setAnnouncement(
      `${movedItem?.label ?? "El acceso"} se ha movido ${
        offset < 0 ? "hacia arriba" : "hacia abajo"
      }.`,
    );
  }

  function handlePointerDown(
    shortcutId: SidebarShortcutId,
    event: ReactPointerEvent<HTMLButtonElement>,
  ) {
    if (!event.isPrimary || event.button !== 0) {
      return;
    }

    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    setDraggingId(shortcutId);
  }

  function handlePointerMove(
    shortcutId: SidebarShortcutId,
    event: ReactPointerEvent<HTMLButtonElement>,
  ) {
    if (
      draggingId !== shortcutId ||
      typeof document.elementFromPoint !== "function"
    ) {
      return;
    }

    const targetElement = document
      .elementFromPoint(event.clientX, event.clientY)
      ?.closest("[data-sidebar-shortcut-id]") as HTMLElement | null;
    const targetId = targetElement?.dataset.sidebarShortcutId as
      | SidebarShortcutId
      | undefined;

    if (targetId && optionalById.has(targetId) && targetId !== shortcutId) {
      swapVisibleShortcuts(shortcutId, targetId);
    }
  }

  function handlePointerUp(
    shortcutId: SidebarShortcutId,
    event: ReactPointerEvent<HTMLButtonElement>,
  ) {
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    setDraggingId(null);
    const item = optionalById.get(shortcutId);
    setAnnouncement(
      `${item?.label ?? "El acceso"} colocado en su nueva posición.`,
    );
  }

  function resetDraft() {
    setDraftIds([...defaultIds]);
    setResetToDefaults(true);
    setError("");
    setAnnouncement(
      "Se han preparado los accesos predeterminados. Guarda para restablecerlos.",
    );
  }

  async function saveDraft() {
    setIsSaving(true);
    setError("");
    try {
      await onSave(draftIds, resetToDefaults);
      onClose();
    } catch (saveError) {
      setError(
        getErrorMessage(
          saveError,
          "No se pudieron guardar tus accesos. Inténtalo de nuevo.",
        ),
      );
    } finally {
      setIsSaving(false);
    }
  }

  function followCatalogLink() {
    onNavigate();
    onClose();
  }

  return (
    <div className="sidebar-dialog-layer">
      <button
        aria-label="Cerrar Todas las secciones"
        className="sidebar-dialog-scrim"
        onClick={onClose}
        type="button"
      />
      <section
        aria-describedby="sidebar-dialog-description"
        aria-labelledby="sidebar-dialog-title"
        aria-modal="true"
        className="sidebar-dialog"
        ref={dialogRef}
        role="dialog"
        tabIndex={-1}
      >
        <header className="sidebar-dialog-header">
          <div>
            <p className="eyebrow">
              {mode === "browse" ? "Navegación" : "Personalización"}
            </p>
            <h2 id="sidebar-dialog-title">
              {mode === "browse" ? "Todas las secciones" : "Mis accesos"}
            </h2>
            <p className="small-muted" id="sidebar-dialog-description">
              {mode === "browse"
                ? "Abre cualquier sección disponible según tus permisos."
                : "Elige y ordena los accesos que quieres ver en la barra lateral."}
            </p>
          </div>
          <button
            aria-label="Cerrar"
            className="sidebar-dialog-close"
            disabled={isSaving}
            onClick={onClose}
            ref={closeButtonRef}
            type="button"
          >
            <X aria-hidden="true" size={19} />
          </button>
        </header>

        {mode === "browse" ? (
          <div className="sidebar-dialog-body sidebar-catalog">
            <section className="sidebar-catalog-section">
              <h3>Principales</h3>
              <div className="sidebar-catalog-grid">
                {fixedItems.map((item) => (
                  <Link
                    className="sidebar-catalog-link"
                    href={item.href}
                    key={item.id}
                    onClick={followCatalogLink}
                  >
                    <SidebarNavIcon
                      name={item.icon as SidebarNavIconName}
                    />
                    <span>
                      <strong>{item.label}</strong>
                      <small>
                        <LockKeyhole aria-hidden="true" size={12} /> Fijado
                      </small>
                    </span>
                  </Link>
                ))}
              </div>
            </section>

            {optionalGroups.map(([group, items]) => (
              <section className="sidebar-catalog-section" key={group}>
                <h3>{group}</h3>
                <div className="sidebar-catalog-grid">
                  {items.map((item) => {
                    const isPinned = selectedIdSet.has(
                      item.id as SidebarShortcutId,
                    );
                    return (
                      <Link
                        className="sidebar-catalog-link"
                        href={item.href}
                        key={item.id}
                        onClick={followCatalogLink}
                      >
                        <SidebarNavIcon
                          name={item.icon as SidebarNavIconName}
                        />
                        <span>
                          <strong>{item.label}</strong>
                          <small>
                            {isPinned ? (
                              <>
                                <Check aria-hidden="true" size={12} /> En Mis
                                accesos
                              </>
                            ) : (
                              "Disponible"
                            )}
                          </small>
                        </span>
                      </Link>
                    );
                  })}
                </div>
              </section>
            ))}

            {utilityItems.length > 0 ? (
              <section className="sidebar-catalog-section">
                <h3>Cuenta</h3>
                <div className="sidebar-catalog-grid">
                  {utilityItems.map((item) => (
                    <Link
                      className="sidebar-catalog-link"
                      href={item.href}
                      key={item.id}
                      onClick={followCatalogLink}
                    >
                      <SidebarNavIcon
                        name={item.icon as SidebarNavIconName}
                      />
                      <span>
                        <strong>{item.label}</strong>
                        <small>
                          <LockKeyhole aria-hidden="true" size={12} /> Siempre
                          disponible
                        </small>
                      </span>
                    </Link>
                  ))}
                </div>
              </section>
            ) : null}
          </div>
        ) : (
          <div className="sidebar-dialog-body sidebar-editor">
            <section className="sidebar-editor-section">
              <div className="sidebar-editor-heading">
                <div>
                  <h3>En la barra lateral</h3>
                  <p className="small-muted">
                    Arrastra desde el asa o usa Subir y Bajar.
                  </p>
                </div>
                <button
                  className="sidebar-reset-button"
                  disabled={isSaving}
                  onClick={resetDraft}
                  type="button"
                >
                  <RotateCcw aria-hidden="true" size={15} /> Restablecer
                </button>
              </div>

              {visibleSelectedItems.length > 0 ? (
                <ol className="sidebar-shortcut-list">
                  {visibleSelectedItems.map((item, index) => {
                    const shortcutId = item.id as SidebarShortcutId;
                    return (
                      <li
                        className={`sidebar-shortcut-row${
                          draggingId === shortcutId ? " is-dragging" : ""
                        }`}
                        data-sidebar-shortcut-id={shortcutId}
                        key={shortcutId}
                      >
                        <button
                          aria-label={`Arrastrar ${item.label}`}
                          className="sidebar-drag-handle"
                          disabled={isSaving}
                          onPointerCancel={(event) =>
                            handlePointerUp(shortcutId, event)
                          }
                          onPointerDown={(event) =>
                            handlePointerDown(shortcutId, event)
                          }
                          onPointerMove={(event) =>
                            handlePointerMove(shortcutId, event)
                          }
                          onPointerUp={(event) =>
                            handlePointerUp(shortcutId, event)
                          }
                          type="button"
                        >
                          <GripVertical aria-hidden="true" size={18} />
                        </button>
                        <SidebarNavIcon
                          name={item.icon as SidebarNavIconName}
                        />
                        <span className="sidebar-shortcut-label">
                          {item.label}
                        </span>
                        <div className="sidebar-shortcut-actions">
                          <button
                            aria-label={`Subir ${item.label}`}
                            disabled={isSaving || index === 0}
                            onClick={() => moveShortcut(shortcutId, -1)}
                            title="Subir"
                            type="button"
                          >
                            <ArrowUp aria-hidden="true" size={16} />
                          </button>
                          <button
                            aria-label={`Bajar ${item.label}`}
                            disabled={
                              isSaving ||
                              index === visibleSelectedItems.length - 1
                            }
                            onClick={() => moveShortcut(shortcutId, 1)}
                            title="Bajar"
                            type="button"
                          >
                            <ArrowDown aria-hidden="true" size={16} />
                          </button>
                          <button
                            aria-label={`Quitar ${item.label}`}
                            disabled={isSaving}
                            onClick={() => removeShortcut(shortcutId)}
                            title="Quitar"
                            type="button"
                          >
                            <X aria-hidden="true" size={16} />
                          </button>
                        </div>
                      </li>
                    );
                  })}
                </ol>
              ) : (
                <p className="sidebar-editor-empty">
                  No has añadido accesos personales. Las secciones principales
                  seguirán disponibles.
                </p>
              )}
            </section>

            <section className="sidebar-editor-section">
              <h3>Añadir secciones</h3>
              {availableGroups.length > 0 ? (
                <div className="sidebar-available-groups">
                  {availableGroups.map(([group, items]) => (
                    <div className="sidebar-available-group" key={group}>
                      <h4>{group}</h4>
                      <div className="sidebar-available-list">
                        {items.map((item) => (
                          <div className="sidebar-available-row" key={item.id}>
                            <SidebarNavIcon
                              name={item.icon as SidebarNavIconName}
                            />
                            <span>{item.label}</span>
                            <button
                              aria-label={`Añadir ${item.label}`}
                              disabled={isSaving}
                              onClick={() =>
                                addShortcut(item.id as SidebarShortcutId)
                              }
                              type="button"
                            >
                              <Plus aria-hidden="true" size={15} /> Añadir
                            </button>
                          </div>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="sidebar-editor-empty">
                  Ya has añadido todas las secciones disponibles.
                </p>
              )}
            </section>
          </div>
        )}

        <p
          aria-live="polite"
          className={`sidebar-dialog-status${error ? " is-error" : ""}`}
        >
          {error || announcement}
        </p>

        <footer className="sidebar-dialog-footer">
          {mode === "browse" ? (
            <>
              <button
                className="secondary-button"
                onClick={onClose}
                type="button"
              >
                Cerrar
              </button>
              <button
                className="accent-button"
                onClick={enterEditMode}
                type="button"
              >
                <Settings2 aria-hidden="true" size={16} /> Personalizar
              </button>
            </>
          ) : (
            <>
              <button
                className="secondary-button"
                disabled={isSaving}
                onClick={cancelEditing}
                type="button"
              >
                Cancelar
              </button>
              <button
                className="accent-button"
                disabled={isSaving}
                onClick={() => void saveDraft()}
                type="button"
              >
                {isSaving ? "Guardando…" : "Guardar cambios"}
              </button>
            </>
          )}
        </footer>
      </section>
    </div>
  );
}
