"use client";

import { useEffect, useRef, useState, type DragEvent, type ReactNode } from "react";
import { ConfirmDialog } from "./ConfirmDialog";

type TownHallEpigraphCardProps = {
  title: string;
  isOpen: boolean;
  canEdit: boolean;
  isSaving: boolean;
  canMoveUp: boolean;
  canMoveDown: boolean;
  onToggle: () => void;
  onRename: (title: string) => void;
  onMoveUp: () => void;
  onMoveDown: () => void;
  onDelete: () => void;
  onDragStart: () => void;
  onDrop: () => void;
  children: ReactNode;
};

function HandleIcon() {
  return (
    <svg aria-hidden="true" fill="currentColor" height="14" viewBox="0 0 24 24" width="14">
      <circle cx="9" cy="6" r="1.6" />
      <circle cx="15" cy="6" r="1.6" />
      <circle cx="9" cy="12" r="1.6" />
      <circle cx="15" cy="12" r="1.6" />
      <circle cx="9" cy="18" r="1.6" />
      <circle cx="15" cy="18" r="1.6" />
    </svg>
  );
}

function MenuIcon({ path, strokeWidth }: { path: string; strokeWidth: number }) {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      height="15"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth={strokeWidth}
      viewBox="0 0 24 24"
      width="15"
    >
      <path d={path} />
    </svg>
  );
}

/**
 * Tarjeta de epígrafe del Ayuntamiento: título plegable, asa que arrastra y
 * abre el menú, y cuerpo con el contenido. La geometría es la del prototipo
 * (docs/diseno-ayuntamiento-prototipo.md §7.2, confirmada en §8.1); el menú
 * lleva las cuatro opciones del diseño completo: renombrar, subir, bajar y
 * eliminar.
 */
export function TownHallEpigraphCard({
  title,
  isOpen,
  canEdit,
  isSaving,
  canMoveUp,
  canMoveDown,
  onToggle,
  onRename,
  onMoveUp,
  onMoveDown,
  onDelete,
  onDragStart,
  onDrop,
  children,
}: TownHallEpigraphCardProps) {
  const [isMenuOpen, setIsMenuOpen] = useState(false);
  const [draft, setDraft] = useState<string | null>(null);
  const [isConfirmingDeletion, setIsConfirmingDeletion] = useState(false);
  const renameInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (draft !== null) {
      renameInputRef.current?.focus();
      renameInputRef.current?.select();
    }
  }, [draft]);

  // El título que llega del servidor manda: al recargarse el árbol se descarta
  // el borrador para no pisar un renombrado de otra sesión.
  useEffect(() => {
    setDraft(null);
  }, [title]);

  function commitRename() {
    const value = draft?.trim();
    setDraft(null);

    if (value !== undefined && value !== "" && value !== title) {
      onRename(value);
    }
  }

  function runFromMenu(action: () => void) {
    setIsMenuOpen(false);
    action();
  }

  return (
    <div
      className="townhall-epigraph"
      onDragOver={(event: DragEvent<HTMLDivElement>) => event.preventDefault()}
      onDrop={(event: DragEvent<HTMLDivElement>) => {
        event.preventDefault();
        onDrop();
      }}
    >
      <div className="townhall-epigraph-head">
        {/* El prototipo usa un <span>; aquí es un botón para que renombrar y
            reordenar también se alcancen con el teclado. */}
        {canEdit ? (
          <button
            aria-expanded={isMenuOpen}
            aria-label={`Opciones del epígrafe ${title}`}
            className="townhall-epigraph-handle"
            draggable
            onClick={() => setIsMenuOpen((current) => !current)}
            onDragStart={onDragStart}
            title="Reordenar · opciones"
            type="button"
          >
            <HandleIcon />
          </button>
        ) : null}

        {draft === null ? (
          <button
            aria-expanded={isOpen}
            className="townhall-epigraph-title"
            draggable={canEdit}
            onClick={onToggle}
            onDragStart={canEdit ? onDragStart : undefined}
            type="button"
          >
            <h2>{title}</h2>
            <svg
              aria-hidden="true"
              className={
                isOpen
                  ? "townhall-epigraph-chevron open"
                  : "townhall-epigraph-chevron"
              }
              fill="none"
              height="18"
              stroke="currentColor"
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth="2"
              viewBox="0 0 24 24"
              width="18"
            >
              <path d="m6 9 6 6 6-6" />
            </svg>
          </button>
        ) : (
          <input
            aria-label={`Nombre del epígrafe ${title}`}
            className="townhall-epigraph-rename"
            disabled={isSaving}
            onBlur={commitRename}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                commitRename();
              } else if (event.key === "Escape") {
                setDraft(null);
              }
            }}
            ref={renameInputRef}
            value={draft}
          />
        )}

        {isMenuOpen ? (
          <>
            <div
              className="townhall-epigraph-scrim"
              onClick={() => setIsMenuOpen(false)}
              role="presentation"
            />
            <div className="townhall-epigraph-menu">
              <button
                disabled={isSaving}
                onClick={() => runFromMenu(() => setDraft(title))}
                type="button"
              >
                <MenuIcon
                  path="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"
                  strokeWidth={1.7}
                />
                Renombrar
              </button>
              <button
                disabled={isSaving || !canMoveUp}
                onClick={() => runFromMenu(onMoveUp)}
                type="button"
              >
                <MenuIcon path="m18 15-6-6-6 6" strokeWidth={1.8} />
                Subir
              </button>
              <button
                disabled={isSaving || !canMoveDown}
                onClick={() => runFromMenu(onMoveDown)}
                type="button"
              >
                <MenuIcon path="m6 9 6 6 6-6" strokeWidth={1.8} />
                Bajar
              </button>
              <button
                className="townhall-epigraph-menu-danger"
                disabled={isSaving}
                onClick={() => runFromMenu(() => setIsConfirmingDeletion(true))}
                type="button"
              >
                <MenuIcon
                  path="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 13h10l1-13"
                  strokeWidth={1.7}
                />
                Eliminar epígrafe
              </button>
            </div>
          </>
        ) : null}
      </div>

      {isOpen ? (
        <div className="townhall-epigraph-body">
          <span aria-hidden="true" className="townhall-epigraph-rule" />
          {children}
        </div>
      ) : null}

      {isConfirmingDeletion ? (
        <ConfirmDialog
          message={`¿Eliminar el epígrafe «${title}» y todo su contenido?`}
          onCancel={() => setIsConfirmingDeletion(false)}
          onConfirm={() => {
            setIsConfirmingDeletion(false);
            onDelete();
          }}
        />
      ) : null}
    </div>
  );
}
