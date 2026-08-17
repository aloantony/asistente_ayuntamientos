"use client";

import Link from "next/link";
import { useCallback, useEffect, useId, useState } from "react";
import { visibleTopNavSections, type TopNavSection } from "../lib/topNav";
import styles from "./TopBar.module.css";

// Barra superior institucional (ADR-034): escudo y nombre del municipio a la
// izquierda, navegación municipal fija en el centro. El bloque de la derecha
// queda reservado para la temperatura del municipio, que llega con el dominio
// de datos municipales.

function ChevronIcon() {
  return (
    <svg
      aria-hidden="true"
      className={styles.chevron}
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

// Escudo por defecto mientras el municipio no tenga uno cargado: un trazo
// neutro, nunca el escudo de otro ayuntamiento.
function CrestPlaceholder() {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      height="40"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.4"
      viewBox="0 0 24 24"
      width="34"
    >
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z" />
    </svg>
  );
}

type TopBarProps = {
  /** Nombre del municipio (o de la organización) mostrado junto al escudo. */
  municipalityName: string;
  /** Escudo del municipio; si falta se dibuja un marcador neutro. */
  crestSrc?: string | null;
  /** Sección activa según la ruta, para resaltarla en la píldora. */
  activeSectionId?: string | null;
};

export function TopBar({
  municipalityName,
  crestSrc,
  activeSectionId,
}: TopBarProps) {
  const sections = visibleTopNavSections();
  const [openSectionId, setOpenSectionId] = useState<string | null>(null);
  const menuIdPrefix = useId();

  const closeMenu = useCallback(() => setOpenSectionId(null), []);

  // Escape cierra el desplegable abierto, como en cualquier menú del shell.
  useEffect(() => {
    if (!openSectionId) {
      return;
    }
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        closeMenu();
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [openSectionId, closeMenu]);

  // Al mover el foco o el puntero fuera de la barra el menú se cierra; sin
  // esto quedaría abierto tras navegar con el teclado.
  function handleBlur(event: React.FocusEvent<HTMLDivElement>) {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
      closeMenu();
    }
  }

  if (sections.length === 0) {
    return null;
  }

  return (
    <div className={styles.topBar}>
      <div className={styles.row}>
        <div className={styles.identity}>
          <span className={styles.crest}>
            {crestSrc ? (
              <img alt="" src={crestSrc} />
            ) : (
              <CrestPlaceholder />
            )}
          </span>
          <span className={styles.municipality} title={municipalityName}>
            {municipalityName}
          </span>
        </div>

        <nav
          aria-label="Navegación municipal"
          className={styles.nav}
          onBlur={handleBlur}
          onMouseLeave={closeMenu}
        >
          {sections.map((section) => (
            <TopNavSectionButton
              isActive={section.id === activeSectionId}
              isOpen={openSectionId === section.id}
              key={section.id}
              menuId={`${menuIdPrefix}-${section.id}`}
              onClose={closeMenu}
              onOpen={() => setOpenSectionId(section.id)}
              onToggle={() =>
                setOpenSectionId((current) =>
                  current === section.id ? null : section.id,
                )
              }
              section={section}
            />
          ))}
        </nav>

        <div className={styles.aside} />
      </div>
    </div>
  );
}

type SectionProps = {
  section: TopNavSection;
  isActive: boolean;
  isOpen: boolean;
  menuId: string;
  onOpen: () => void;
  onToggle: () => void;
  onClose: () => void;
};

function TopNavSectionButton({
  section,
  isActive,
  isOpen,
  menuId,
  onOpen,
  onToggle,
  onClose,
}: SectionProps) {
  const [pointerInside, setPointerInside] = useState(false);
  const hasMenu = section.items.length > 0;
  const className = `${styles.sectionButton}${
    isActive ? ` ${styles.sectionButtonActive}` : ""
  }`;

  // Sin submenú la sección es un enlace normal: no hay nada que desplegar.
  if (!hasMenu) {
    return (
      <div className={styles.section}>
        <Link
          aria-current={isActive ? "page" : undefined}
          className={className}
          href={section.href ?? "#"}
          onMouseEnter={onClose}
        >
          {section.label}
        </Link>
      </div>
    );
  }

  // El puntero ya abre el desplegable al entrar, así que el clic que sigue no
  // debe cerrarlo: sólo alterna cuando la activación llega por teclado, donde
  // el botón no ha recibido ningún mouseenter previo.
  function handleClick() {
    if (pointerInside) {
      onOpen();
      return;
    }
    onToggle();
  }

  return (
    <div
      className={styles.section}
      onMouseEnter={() => {
        setPointerInside(true);
        onOpen();
      }}
      onMouseLeave={() => setPointerInside(false)}
    >
      <button
        aria-controls={isOpen ? menuId : undefined}
        aria-expanded={isOpen}
        aria-haspopup="true"
        className={className}
        onClick={handleClick}
        onFocus={onOpen}
        type="button"
      >
        {section.label}
        <ChevronIcon />
      </button>
      {isOpen ? (
        <div className={styles.menu} id={menuId}>
          <div className={styles.menuInner}>
            {section.items.map((item) => (
              <Link
                className={styles.menuItem}
                href={item.href}
                key={`${item.label}-${item.href}`}
                onClick={onClose}
              >
                {item.label}
              </Link>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
