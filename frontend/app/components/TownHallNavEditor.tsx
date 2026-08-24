"use client";

import { useEffect, useState } from "react";
import { ConfirmDialog } from "./ConfirmDialog";
import type { TownHall, TownHallNavSection } from "./types";

type DraggedBlock = { kind: "section" | "item"; id: number };

type PendingDeletion = { id: number; title: string; kind: "section" | "item" };

type TownHallNavEditorProps = {
  townHall: TownHall;
  isSaving: boolean;
  fallbackName: string;
  onClose: () => void;
  onRenameMunicipality: (name: string) => void;
  onToggleWeather: (enabled: boolean) => void;
  onChangeWeatherLocation: (location: string) => void;
  onAddSection: () => void;
  onAddItem: (sectionId: number) => void;
  onRenameBlock: (blockId: number, title: string) => void;
  onArchiveBlock: (blockId: number) => void;
  onReorder: (nav: TownHallNavSection[]) => void;
};

function moveSection(
  nav: TownHallNavSection[],
  draggedId: number,
  targetId: number,
) {
  const from = nav.findIndex((section) => section.id === draggedId);
  const to = nav.findIndex((section) => section.id === targetId);

  if (from === -1 || to === -1 || from === to) {
    return null;
  }

  const next = [...nav];
  const [moved] = next.splice(from, 1);
  next.splice(to, 0, moved);
  return next;
}

/** Mueve un epígrafe a otra pestaña, opcionalmente ante un epígrafe concreto. */
function moveItem(
  nav: TownHallNavSection[],
  draggedId: number,
  targetSectionId: number,
  targetItemId: number | null,
) {
  const source = nav.find((section) =>
    section.items.some((item) => item.id === draggedId),
  );
  const moved = source?.items.find((item) => item.id === draggedId);

  if (source === undefined || moved === undefined) {
    return null;
  }

  if (source.id === targetSectionId && targetItemId === draggedId) {
    return null;
  }

  return nav.map((section) => {
    const items = section.items.filter((item) => item.id !== draggedId);

    if (section.id !== targetSectionId) {
      return { ...section, items };
    }

    const index =
      targetItemId === null
        ? items.length
        : items.findIndex((item) => item.id === targetItemId);

    items.splice(index === -1 ? items.length : index, 0, moved);
    return { ...section, items };
  });
}

function DragHandleIcon({ size }: { size: number }) {
  const radius = size >= 14 ? 1.6 : 1.5;

  return (
    <svg aria-hidden="true" fill="currentColor" height={size} viewBox="0 0 24 24" width={size}>
      <circle cx="9" cy="6" r={radius} />
      <circle cx="15" cy="6" r={radius} />
      <circle cx="9" cy="12" r={radius} />
      <circle cx="15" cy="12" r={radius} />
      <circle cx="9" cy="18" r={radius} />
      <circle cx="15" cy="18" r={radius} />
    </svg>
  );
}

function PlusIcon({ size }: { size: number }) {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      height={size}
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="2"
      viewBox="0 0 24 24"
      width={size}
    >
      <path d="M12 5v14M5 12h14" />
    </svg>
  );
}

export function TownHallNavEditor({
  townHall,
  isSaving,
  fallbackName,
  onClose,
  onRenameMunicipality,
  onToggleWeather,
  onChangeWeatherLocation,
  onAddSection,
  onAddItem,
  onRenameBlock,
  onArchiveBlock,
  onReorder,
}: TownHallNavEditorProps) {
  const [municipalityName, setMunicipalityName] = useState(
    townHall.profile.display_name ?? "",
  );
  const [weatherLocation, setWeatherLocation] = useState(
    townHall.profile.weather_location ?? "",
  );
  const [titles, setTitles] = useState<Record<number, string>>({});
  const [dragged, setDragged] = useState<DraggedBlock | null>(null);
  const [pendingDeletion, setPendingDeletion] = useState<PendingDeletion | null>(
    null,
  );

  // Los borradores locales sólo espejan lo que llega del servidor; al recargar
  // el árbol se descartan para no pisar renombrados de otra sesión.
  useEffect(() => {
    setTitles({});
  }, [townHall]);

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape" && pendingDeletion === null) {
        onClose();
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [onClose, pendingDeletion]);

  function titleValue(blockId: number, serverTitle: string) {
    return titles[blockId] ?? serverTitle;
  }

  function commitTitle(blockId: number, serverTitle: string) {
    const draft = titles[blockId]?.trim();

    if (draft === undefined || draft === serverTitle) {
      return;
    }

    if (draft === "") {
      // Una pestaña sin nombre no es editable después: se revierte.
      setTitles((current) => {
        const next = { ...current };
        delete next[blockId];
        return next;
      });
      return;
    }

    onRenameBlock(blockId, draft);
  }

  function commitWeatherLocation() {
    const draft = weatherLocation.trim();

    if (draft === (townHall.profile.weather_location ?? "")) {
      return;
    }

    onChangeWeatherLocation(draft);
  }

  function commitMunicipalityName() {
    const draft = municipalityName.trim();

    if (draft === (townHall.profile.display_name ?? "")) {
      return;
    }

    onRenameMunicipality(draft);
  }

  function handleDropOnSection(targetSectionId: number) {
    if (dragged === null) {
      return;
    }

    const next =
      dragged.kind === "section"
        ? moveSection(townHall.nav, dragged.id, targetSectionId)
        : moveItem(townHall.nav, dragged.id, targetSectionId, null);

    setDragged(null);

    if (next !== null) {
      onReorder(next);
    }
  }

  function handleDropOnItem(targetSectionId: number, targetItemId: number) {
    if (dragged === null || dragged.kind !== "item") {
      return;
    }

    const next = moveItem(
      townHall.nav,
      dragged.id,
      targetSectionId,
      targetItemId,
    );
    setDragged(null);

    if (next !== null) {
      onReorder(next);
    }
  }

  return (
    <div className="townhall-editor-scrim" onClick={onClose} role="presentation">
      <div
        aria-modal="true"
        aria-label="Gestionar pestañas"
        className="townhall-editor"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
      >
        <div className="townhall-editor-head">
          <h3>Gestionar pestañas</h3>
          <button
            aria-label="Cerrar"
            className="townhall-editor-close"
            onClick={onClose}
            type="button"
          >
            <svg
              aria-hidden="true"
              fill="none"
              height="16"
              stroke="currentColor"
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth="1.8"
              viewBox="0 0 24 24"
              width="16"
            >
              <path d="M6 6l12 12M18 6 6 18" />
            </svg>
          </button>
        </div>

        <div className="townhall-editor-card">
          <span className="townhall-editor-eyebrow">Municipio</span>
          <label className="townhall-editor-label" htmlFor="townhall-name-input">
            Nombre
          </label>
          <input
            className="townhall-editor-name-input"
            disabled={isSaving}
            id="townhall-name-input"
            onBlur={commitMunicipalityName}
            onChange={(event) => setMunicipalityName(event.target.value)}
            placeholder={fallbackName}
            value={municipalityName}
          />
          <p className="muted">
            Para cambiar el escudo, arrastra una imagen sobre el escudo de la
            barra de navegación.
          </p>
        </div>

        <span className="townhall-editor-eyebrow">Pestañas y epígrafes</span>

        {townHall.nav.map((section) => (
          <div
            className="townhall-editor-section"
            key={section.id}
            onDragOver={(event) => event.preventDefault()}
            onDrop={() => handleDropOnSection(section.id)}
          >
            <div className="townhall-editor-row">
              <span
                className="townhall-editor-handle"
                draggable
                onDragStart={() => setDragged({ kind: "section", id: section.id })}
                title="Arrastrar para reordenar"
              >
                <DragHandleIcon size={14} />
              </span>
              <input
                aria-label={`Nombre de la pestaña ${section.title}`}
                className="townhall-editor-section-input"
                disabled={isSaving}
                onBlur={() => commitTitle(section.id, section.title)}
                onChange={(event) =>
                  setTitles((current) => ({
                    ...current,
                    [section.id]: event.target.value,
                  }))
                }
                value={titleValue(section.id, section.title)}
              />
              <button
                aria-label={`Eliminar la pestaña ${section.title}`}
                className="townhall-editor-delete"
                disabled={isSaving}
                onClick={() =>
                  setPendingDeletion({
                    id: section.id,
                    title: section.title,
                    kind: "section",
                  })
                }
                type="button"
              >
                <svg
                  aria-hidden="true"
                  fill="none"
                  height="15"
                  stroke="currentColor"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth="1.7"
                  viewBox="0 0 24 24"
                  width="15"
                >
                  <path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 13h10l1-13" />
                </svg>
              </button>
            </div>

            <div className="townhall-editor-items">
              {section.items.map((item) => (
                <div
                  className="townhall-editor-row"
                  key={item.id}
                  onDragOver={(event) => event.preventDefault()}
                  onDrop={(event) => {
                    event.stopPropagation();
                    handleDropOnItem(section.id, item.id);
                  }}
                >
                  <span
                    className="townhall-editor-handle"
                    draggable
                    onDragStart={() => setDragged({ kind: "item", id: item.id })}
                    title="Arrastrar para reordenar"
                  >
                    <DragHandleIcon size={12} />
                  </span>
                  <input
                    aria-label={`Nombre del epígrafe ${item.title}`}
                    className="townhall-editor-item-input"
                    disabled={isSaving}
                    onBlur={() => commitTitle(item.id, item.title)}
                    onChange={(event) =>
                      setTitles((current) => ({
                        ...current,
                        [item.id]: event.target.value,
                      }))
                    }
                    value={titleValue(item.id, item.title)}
                  />
                  <button
                    aria-label={`Eliminar el epígrafe ${item.title}`}
                    className="townhall-editor-delete small"
                    disabled={isSaving}
                    onClick={() =>
                      setPendingDeletion({
                        id: item.id,
                        title: item.title,
                        kind: "item",
                      })
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
              ))}
              <button
                className="townhall-editor-add-item"
                disabled={isSaving}
                onClick={() => onAddItem(section.id)}
                type="button"
              >
                <PlusIcon size={13} />
                Añadir epígrafe
              </button>
            </div>
          </div>
        ))}

        <button
          className="townhall-editor-add-section"
          disabled={isSaving}
          onClick={onAddSection}
          type="button"
        >
          <PlusIcon size={15} />
          Añadir pestaña
        </button>

        <div className="townhall-editor-card townhall-editor-weather">
          <div>
            <span className="townhall-editor-eyebrow">Bloque de temperatura</span>
            <span className="townhall-editor-weather-hint">
              Muestra la temperatura del municipio en la barra.
            </span>
          </div>
          <button
            aria-pressed={townHall.profile.weather_enabled}
            className="townhall-editor-toggle"
            disabled={isSaving}
            onClick={() => onToggleWeather(!townHall.profile.weather_enabled)}
            type="button"
          >
            {townHall.profile.weather_enabled ? "Activado" : "Desactivado"}
          </button>
        </div>

        {townHall.profile.weather_enabled ? (
          <div className="townhall-editor-card">
            <label
              className="townhall-editor-label"
              htmlFor="townhall-weather-location"
            >
              Localidad de la que se consulta la temperatura
            </label>
            <input
              className="townhall-editor-item-input"
              disabled={isSaving}
              id="townhall-weather-location"
              onBlur={commitWeatherLocation}
              onChange={(event) => setWeatherLocation(event.target.value)}
              placeholder={fallbackName}
              value={weatherLocation}
            />
            <p className="muted">
              Se consulta a Open-Meteo desde el servidor. Solo sale de aquí el
              nombre de la localidad, una vez, para situarla en el mapa.
            </p>
          </div>
        ) : null}

        <button
          className="accent-button townhall-editor-done"
          onClick={onClose}
          type="button"
        >
          Hecho
        </button>
      </div>

      {pendingDeletion !== null ? (
        <ConfirmDialog
          message={
            pendingDeletion.kind === "section"
              ? `¿Eliminar la pestaña «${pendingDeletion.title}» y todos sus epígrafes?`
              : `¿Eliminar el epígrafe «${pendingDeletion.title}»?`
          }
          onCancel={() => setPendingDeletion(null)}
          onConfirm={() => {
            onArchiveBlock(pendingDeletion.id);
            setPendingDeletion(null);
          }}
        />
      ) : null}
    </div>
  );
}
