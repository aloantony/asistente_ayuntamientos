"use client";

import { useState } from "react";
import type { TownHall } from "./types";

type TownHallBarProps = {
  townHall: TownHall;
  activeId: number | null;
  canEdit: boolean;
  fallbackName: string;
  shieldUrl: string | null;
  onSelect: (blockId: number) => void;
  onOpenEditor: () => void;
  onShieldDrop: (file: File) => void;
};

function ChevronIcon() {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      height="12"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="2"
      viewBox="0 0 24 24"
      width="12"
    >
      <path d="m6 9 6 6 6-6" />
    </svg>
  );
}

function MenuDotsIcon() {
  return (
    <svg aria-hidden="true" fill="currentColor" height="16" viewBox="0 0 24 24" width="16">
      <circle cx="9" cy="5" r="1.7" />
      <circle cx="15" cy="5" r="1.7" />
      <circle cx="9" cy="12" r="1.7" />
      <circle cx="15" cy="12" r="1.7" />
      <circle cx="9" cy="19" r="1.7" />
      <circle cx="15" cy="19" r="1.7" />
    </svg>
  );
}

export function TownHallBar({
  townHall,
  activeId,
  canEdit,
  fallbackName,
  shieldUrl,
  onSelect,
  onOpenEditor,
  onShieldDrop,
}: TownHallBarProps) {
  const [openSectionId, setOpenSectionId] = useState<number | null>(null);
  const [isShieldTargeted, setIsShieldTargeted] = useState(false);

  const municipalityName =
    townHall.profile.display_name?.trim() || fallbackName;
  const weatherLabel =
    townHall.profile.weather_location?.trim() || municipalityName;

  function handleSectionClick(sectionId: number, hasItems: boolean) {
    onSelect(sectionId);
    // Con teclado no hay hover: el propio clic abre y cierra el desplegable.
    setOpenSectionId((current) =>
      hasItems && current !== sectionId ? sectionId : null,
    );
  }

  function handleShieldDrop(event: React.DragEvent) {
    event.preventDefault();
    setIsShieldTargeted(false);

    const file = event.dataTransfer.files?.[0];
    if (canEdit && file && file.type.startsWith("image/")) {
      onShieldDrop(file);
    }
  }

  return (
    <div className="townhall-bar">
      <div className="townhall-bar-brand">
        <span
          aria-hidden="true"
          className={
            isShieldTargeted
              ? "townhall-shield targeted"
              : "townhall-shield"
          }
          onDragLeave={() => setIsShieldTargeted(false)}
          onDragOver={(event) => {
            if (!canEdit) {
              return;
            }
            event.preventDefault();
            setIsShieldTargeted(true);
          }}
          onDrop={handleShieldDrop}
          title={
            canEdit
              ? "Arrastra una imagen para cambiar el escudo"
              : undefined
          }
        >
          <img alt="" src={shieldUrl ?? "/brand/logo-principal.svg"} />
        </span>
        <span className="townhall-name" title={municipalityName}>
          {municipalityName}
        </span>
      </div>

      <nav aria-label="Secciones del Ayuntamiento" className="townhall-nav">
        {townHall.nav.map((section) => {
          const hasItems = section.items.length > 0;
          const isOpen = hasItems && openSectionId === section.id;
          const isActive =
            activeId === section.id ||
            section.items.some((item) => item.id === activeId);

          return (
            <div
              className="townhall-nav-group"
              key={section.id}
              onMouseEnter={() => setOpenSectionId(hasItems ? section.id : null)}
              onMouseLeave={() => setOpenSectionId(null)}
            >
              <button
                aria-expanded={hasItems ? isOpen : undefined}
                className={
                  isActive
                    ? "townhall-nav-button active"
                    : "townhall-nav-button"
                }
                onClick={() => handleSectionClick(section.id, hasItems)}
                type="button"
              >
                <span>{section.title}</span>
                {hasItems ? <ChevronIcon /> : null}
              </button>
              {isOpen ? (
                <div className="townhall-nav-menu-anchor">
                  <div className="townhall-nav-menu">
                    {section.items.map((item) => (
                      <button
                        className={
                          activeId === item.id
                            ? "townhall-nav-menu-item active"
                            : "townhall-nav-menu-item"
                        }
                        key={item.id}
                        onClick={() => {
                          onSelect(item.id);
                          setOpenSectionId(null);
                        }}
                        type="button"
                      >
                        {item.title}
                      </button>
                    ))}
                  </div>
                </div>
              ) : null}
            </div>
          );
        })}
      </nav>

      <div className="townhall-bar-actions">
        {townHall.profile.weather_enabled ? (
          <div
            className="townhall-weather"
            title={`Sin fuente de datos meteorológicos para ${weatherLabel}`}
          >
            <svg
              aria-hidden="true"
              fill="none"
              height="20"
              stroke="var(--text-muted)"
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth="1.6"
              viewBox="0 0 24 24"
              width="20"
            >
              <path d="M6.5 18a4.5 4.5 0 0 1-.5-8.97A6 6 0 0 1 17.7 10.3 3.85 3.85 0 0 1 17 18H6.5Z" />
            </svg>
            {/* Sin fuente configurada no se inventa una cifra. */}
            <span className="townhall-weather-value">—</span>
          </div>
        ) : null}
        {canEdit ? (
          <button
            aria-label="Editar menú de navegación"
            className={
              townHall.profile.weather_enabled
                ? "townhall-editor-button inline"
                : "townhall-editor-button"
            }
            onClick={onOpenEditor}
            title="Editar menú"
            type="button"
          >
            <MenuDotsIcon />
          </button>
        ) : null}
      </div>
    </div>
  );
}
