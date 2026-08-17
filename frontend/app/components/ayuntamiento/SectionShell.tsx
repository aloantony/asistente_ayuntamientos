"use client";

import { useCallback, useEffect, useId, useState, type ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import styles from "./SectionShell.module.css";

// Sección colapsable de la pantalla del ayuntamiento. Las secciones de esta
// pantalla son largas y desiguales —unas tienen tres campos y otras una tabla
// entera—, así que cada una recuerda si el usuario la dejó abierta. La
// preferencia es por sección y por navegador, como el resto de ajustes locales
// (ADR-012); si el almacenamiento no está disponible, el estado dura la sesión.

const STORAGE_PREFIX = "anacleto:ayuntamiento:seccion:v1";

function storageKeyFor(sectionKey: string) {
  return `${STORAGE_PREFIX}:${sectionKey}`;
}

function ChevronIcon({ isOpen }: { isOpen: boolean }) {
  return (
    <svg
      aria-hidden="true"
      className={`${styles.chevron}${isOpen ? ` ${styles.chevronOpen}` : ""}`}
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
  );
}

type SectionShellProps = {
  /** Clave estable de la sección; identifica su preferencia guardada. */
  sectionKey: string;
  title: string;
  /** Icono opcional junto al título. */
  icon?: LucideIcon;
  /** Conteo real mostrado junto al título (nunca una cifra estimada). */
  count?: number | null;
  /** Estado inicial cuando el usuario todavía no ha elegido. */
  defaultOpen?: boolean;
  children: ReactNode;
};

export function SectionShell({
  sectionKey,
  title,
  icon: Icon,
  count,
  defaultOpen = true,
  children,
}: SectionShellProps) {
  const [isOpen, setIsOpen] = useState(defaultOpen);
  const bodyId = useId();

  // La preferencia se lee tras montar: leerla durante el render desincronizaría
  // el marcado del servidor con el del cliente.
  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(storageKeyFor(sectionKey));
      if (stored === "open" || stored === "closed") {
        setIsOpen(stored === "open");
      }
    } catch {
      // Sin almacenamiento la sección sigue el valor por defecto.
    }
  }, [sectionKey]);

  const toggle = useCallback(() => {
    setIsOpen((open) => {
      const next = !open;
      try {
        window.localStorage.setItem(
          storageKeyFor(sectionKey),
          next ? "open" : "closed",
        );
      } catch {
        // Plegar y desplegar sigue funcionando durante la sesión.
      }
      return next;
    });
  }, [sectionKey]);

  return (
    <section className={styles.section}>
      <button
        aria-controls={bodyId}
        aria-expanded={isOpen}
        className={styles.header}
        onClick={toggle}
        type="button"
      >
        <span className={styles.heading}>
          {Icon ? (
            <Icon
              aria-hidden="true"
              className={styles.icon}
              size={18}
              strokeWidth={1.6}
            />
          ) : null}
          <h2 className={styles.title}>{title}</h2>
          {typeof count === "number" ? (
            <span className={styles.count}>{count}</span>
          ) : null}
        </span>
        <ChevronIcon isOpen={isOpen} />
      </button>
      {isOpen ? (
        <div className={styles.body} id={bodyId}>
          {children}
        </div>
      ) : null}
    </section>
  );
}
