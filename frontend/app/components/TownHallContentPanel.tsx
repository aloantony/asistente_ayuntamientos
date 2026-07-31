"use client";

import { useEffect, useState } from "react";
import { ConfirmDialog } from "./ConfirmDialog";
import type { TownHallContent, TownHallSectionLayout } from "./types";

type TownHallContentPanelProps = {
  content: TownHallContent;
  canEdit: boolean;
  isSaving: boolean;
  onAdd: () => void;
  onSaveTitle: (itemId: number, title: string) => void;
  onSaveBody: (itemId: number, body: string) => void;
  onArchive: (itemId: number) => void;
  onChangeLayout: (layout: TownHallSectionLayout) => void;
};

/**
 * Contenido de un apartado del Ayuntamiento: una lista de elementos con título
 * y texto. Se edita con campos normales y guardado al perder el foco, no con
 * `contenteditable` como el prototipo (decisión 2 de
 * docs/diseno-ayuntamiento-prototipo.md).
 */
export function TownHallContentPanel({
  content,
  canEdit,
  isSaving,
  onAdd,
  onSaveTitle,
  onSaveBody,
  onArchive,
  onChangeLayout,
}: TownHallContentPanelProps) {
  const [drafts, setDrafts] = useState<Record<number, string>>({});
  const [bodies, setBodies] = useState<Record<number, string>>({});
  const [pendingDeletion, setPendingDeletion] = useState<{
    id: number;
    title: string;
  } | null>(null);

  // Los borradores locales sólo espejan lo que llega del servidor; al recargar
  // el apartado se descartan para no pisar cambios de otra sesión.
  useEffect(() => {
    setDrafts({});
    setBodies({});
  }, [content]);

  function commit(
    itemId: number,
    draft: string | undefined,
    stored: string,
    save: (itemId: number, value: string) => void,
    clear: () => void,
  ) {
    if (draft === undefined || draft.trim() === stored.trim()) {
      return;
    }
    if (draft.trim() === "" && stored !== "") {
      // Un elemento sin título dejaría de ser editable: se revierte.
      clear();
      return;
    }
    save(itemId, draft);
  }

  const isContacts = content.layout === "contacts";

  return (
    <article
      className={
        isContacts ? "townhall-content townhall-content-contacts" : "townhall-content"
      }
    >
      <header className="townhall-content-head">
        <p className="eyebrow">
          {content.parent_title
            ? `${content.parent_title} · ${content.title}`
            : content.title}
        </p>
        <div className="townhall-content-head-row">
          <h2>{content.title}</h2>
          {canEdit ? (
            <label className="townhall-content-layout">
              Formato
              <select
                disabled={isSaving}
                onChange={(event) =>
                  onChangeLayout(event.target.value as TownHallSectionLayout)
                }
                value={content.layout}
              >
                <option value="text">Texto</option>
                <option value="contacts">Teléfonos</option>
              </select>
            </label>
          ) : null}
        </div>
      </header>

      {content.items.length === 0 ? (
        <p className="muted">
          {canEdit
            ? "Este apartado todavía no tiene elementos. Añade el primero."
            : "Este apartado todavía no tiene contenido."}
        </p>
      ) : null}

      <div className="townhall-content-items">
        {content.items.map((item) => (
          <section className="townhall-content-item" key={item.id}>
            {canEdit ? (
              <div className="townhall-content-item-head">
                <input
                  aria-label={`Título de ${item.title}`}
                  className="townhall-content-title-input"
                  disabled={isSaving}
                  onBlur={() =>
                    commit(
                      item.id,
                      drafts[item.id],
                      item.title,
                      onSaveTitle,
                      () =>
                        setDrafts((current) => {
                          const next = { ...current };
                          delete next[item.id];
                          return next;
                        }),
                    )
                  }
                  onChange={(event) =>
                    setDrafts((current) => ({
                      ...current,
                      [item.id]: event.target.value,
                    }))
                  }
                  value={drafts[item.id] ?? item.title}
                />
                <button
                  aria-label={`Eliminar ${item.title}`}
                  className="townhall-editor-delete small"
                  disabled={isSaving}
                  onClick={() =>
                    setPendingDeletion({ id: item.id, title: item.title })
                  }
                  type="button"
                >
                  <svg
                    aria-hidden="true"
                    fill="none"
                    height="13"
                    stroke="currentColor"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth="1.8"
                    viewBox="0 0 24 24"
                    width="13"
                  >
                    <path d="M6 6l12 12M18 6 6 18" />
                  </svg>
                </button>
              </div>
            ) : (
              <h3>{item.title}</h3>
            )}

            {canEdit ? (
              isContacts ? (
                <input
                  aria-label={`Teléfono de ${item.title}`}
                  className="townhall-content-phone-input"
                  disabled={isSaving}
                  inputMode="tel"
                  onBlur={() =>
                    commit(
                      item.id,
                      bodies[item.id],
                      item.body ?? "",
                      onSaveBody,
                      () =>
                        setBodies((current) => {
                          const next = { ...current };
                          delete next[item.id];
                          return next;
                        }),
                    )
                  }
                  onChange={(event) =>
                    setBodies((current) => ({
                      ...current,
                      [item.id]: event.target.value,
                    }))
                  }
                  placeholder="947 00 00 00"
                  type="tel"
                  value={bodies[item.id] ?? item.body ?? ""}
                />
              ) : (
              <textarea
                aria-label={`Texto de ${item.title}`}
                className="townhall-content-body-input"
                disabled={isSaving}
                onBlur={() =>
                  commit(
                    item.id,
                    bodies[item.id],
                    item.body ?? "",
                    onSaveBody,
                    () =>
                      setBodies((current) => {
                        const next = { ...current };
                        delete next[item.id];
                        return next;
                      }),
                  )
                }
                onChange={(event) =>
                  setBodies((current) => ({
                    ...current,
                    [item.id]: event.target.value,
                  }))
                }
                placeholder="Escribe aquí el contenido de este elemento…"
                rows={4}
                value={bodies[item.id] ?? item.body ?? ""}
              />
              )
            ) : isContacts && item.body ? (
              // Un número marcable: en el móvil del alcalde esto importa.
              <a className="townhall-content-phone" href={`tel:${item.body.replace(/\s+/g, "")}`}>
                {item.body}
              </a>
            ) : (
              <p className="townhall-content-body">
                {item.body || "Sin contenido todavía."}
              </p>
            )}
          </section>
        ))}
      </div>

      {canEdit ? (
        <button
          className="townhall-editor-add-section"
          disabled={isSaving}
          onClick={onAdd}
          type="button"
        >
          {isContacts ? "Añadir teléfono" : "Añadir elemento"}
        </button>
      ) : null}

      {pendingDeletion !== null ? (
        <ConfirmDialog
          message={`¿Eliminar el elemento «${pendingDeletion.title}»?`}
          onCancel={() => setPendingDeletion(null)}
          onConfirm={() => {
            onArchive(pendingDeletion.id);
            setPendingDeletion(null);
          }}
        />
      ) : null}
    </article>
  );
}
