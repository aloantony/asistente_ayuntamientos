"use client";

import { useEffect, useState } from "react";
import { townHallAttachmentUrl } from "../lib/api";
import { ConfirmDialog } from "./ConfirmDialog";
import type {
  TownHallContentField,
  TownHallContent,
  TownHallSectionLayout,
} from "./types";

type TownHallContentPanelProps = {
  content: TownHallContent;
  canEdit: boolean;
  isSaving: boolean;
  onAdd: () => void;
  onSaveTitle: (itemId: number, title: string) => void;
  onSaveBody: (itemId: number, body: string) => void;
  onArchive: (itemId: number) => void;
  onChangeLayout: (layout: TownHallSectionLayout) => void;
  onSaveFields: (itemId: number, fields: TownHallContentField[]) => void;
  onAddAttachment: (itemId: number, file: File) => void;
  onRemoveAttachment: (itemId: number, index: number) => void;
};

/**
 * Contenido de un apartado del Ayuntamiento: una lista de elementos con título
 * y texto. Se edita con campos normales y guardado al perder el foco, no con
 * `contenteditable` como el prototipo (decisión 2 de
 * docs/diseno-ayuntamiento-prototipo.md).
 */
function formatSize(bytes: number) {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${Math.round(bytes / 1024)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function TownHallContentPanel({
  content,
  canEdit,
  isSaving,
  onAdd,
  onSaveTitle,
  onSaveBody,
  onArchive,
  onChangeLayout,
  onSaveFields,
  onAddAttachment,
  onRemoveAttachment,
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
  const isPeople = content.layout === "people";
  const isFiles = content.layout === "files";

  return (
    <article
      className={
        isContacts
          ? "townhall-content townhall-content-contacts"
          : isPeople
            ? "townhall-content townhall-content-people"
            : isFiles
              ? "townhall-content townhall-content-files"
              : "townhall-content"
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
                <option value="people">Personas</option>
                <option value="files">Archivo</option>
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

            {isPeople ? (
              <div className="townhall-content-fields">
                {item.fields.map((field, index) => (
                  <div className="townhall-content-field" key={`${item.id}-${index}`}>
                    {canEdit ? (
                      <>
                        <input
                          aria-label={`Nombre del campo ${index + 1} de ${item.title}`}
                          className="townhall-content-field-label"
                          disabled={isSaving}
                          onBlur={(event) => {
                            const label = event.target.value.trim();
                            if (label === field.label) {
                              return;
                            }
                            if (label === "") {
                              event.target.value = field.label;
                              return;
                            }
                            onSaveFields(
                              item.id,
                              item.fields.map((current, position) =>
                                position === index
                                  ? { ...current, label }
                                  : current,
                              ),
                            );
                          }}
                          defaultValue={field.label}
                        />
                        <input
                          aria-label={`${field.label} de ${item.title}`}
                          className="townhall-content-field-value"
                          disabled={isSaving}
                          onBlur={(event) => {
                            const value = event.target.value;
                            if (value === field.value) {
                              return;
                            }
                            onSaveFields(
                              item.id,
                              item.fields.map((current, position) =>
                                position === index
                                  ? { ...current, value }
                                  : current,
                              ),
                            );
                          }}
                          defaultValue={field.value}
                        />
                        <button
                          aria-label={`Eliminar el campo ${field.label}`}
                          className="townhall-editor-delete small"
                          disabled={isSaving}
                          onClick={() =>
                            onSaveFields(
                              item.id,
                              item.fields.filter(
                                (_current, position) => position !== index,
                              ),
                            )
                          }
                          type="button"
                        >
                          <svg
                            aria-hidden="true"
                            fill="none"
                            height="12"
                            stroke="currentColor"
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            strokeWidth="1.8"
                            viewBox="0 0 24 24"
                            width="12"
                          >
                            <path d="M6 6l12 12M18 6 6 18" />
                          </svg>
                        </button>
                      </>
                    ) : (
                      <>
                        <span className="townhall-content-field-label-text">
                          {field.label}
                        </span>
                        <span>{field.value || "—"}</span>
                      </>
                    )}
                  </div>
                ))}
                {canEdit ? (
                  <button
                    className="townhall-editor-add-item"
                    disabled={isSaving}
                    onClick={() =>
                      onSaveFields(item.id, [
                        ...item.fields,
                        { label: `Campo ${item.fields.length + 1}`, value: "" },
                      ])
                    }
                    type="button"
                  >
                    Añadir campo
                  </button>
                ) : null}
              </div>
            ) : canEdit ? (
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
            {isFiles ? (
              <div className="townhall-attachments">
                {item.attachments.map((attachment) => (
                  <div className="townhall-attachment" key={attachment.index}>
                    <a
                      download
                      href={townHallAttachmentUrl(item.id, attachment.index)}
                    >
                      {attachment.name}
                    </a>
                    <span>{formatSize(attachment.size_bytes)}</span>
                    {canEdit ? (
                      <button
                        aria-label={`Eliminar ${attachment.name}`}
                        className="townhall-editor-delete small"
                        disabled={isSaving}
                        onClick={() =>
                          onRemoveAttachment(item.id, attachment.index)
                        }
                        type="button"
                      >
                        <svg
                          aria-hidden="true"
                          fill="none"
                          height="12"
                          stroke="currentColor"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          strokeWidth="1.8"
                          viewBox="0 0 24 24"
                          width="12"
                        >
                          <path d="M6 6l12 12M18 6 6 18" />
                        </svg>
                      </button>
                    ) : null}
                  </div>
                ))}
                {item.attachments.length === 0 && !canEdit ? (
                  <p className="muted">Sin adjuntos.</p>
                ) : null}
                {canEdit ? (
                  <label className="townhall-attachment-add">
                    Añadir archivo
                    <input
                      accept=".pdf,.png,.jpg,.jpeg,.webp,.mp3,.ogg"
                      disabled={isSaving}
                      onChange={(event) => {
                        const file = event.target.files?.[0];
                        if (file) {
                          onAddAttachment(item.id, file);
                        }
                        // Permite volver a elegir el mismo fichero.
                        event.target.value = "";
                      }}
                      type="file"
                    />
                  </label>
                ) : null}
              </div>
            ) : null}
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
          {isContacts
            ? "Añadir teléfono"
            : isPeople
              ? "Añadir persona"
              : isFiles
                ? "Añadir entrada"
                : "Añadir elemento"}
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
