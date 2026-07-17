"use client";

import { ExternalLink, X } from "lucide-react";
import dynamic from "next/dynamic";
import { useEffect, useRef, type MouseEvent } from "react";
import {
  buildAssistantAppViewHref,
  type AssistantAppView,
} from "../lib/assistantAppViews";
import type { User } from "./types";

const EmbeddedMapPanel = dynamic(
  () => import("./MapPanel").then((module) => module.MapPanel),
  { loading: () => <p className="muted">Cargando mapa…</p> },
);
const EmbeddedRequirementsWorkspace = dynamic(
  () =>
    import("./EmbeddedRequirementsWorkspace").then(
      (module) => module.EmbeddedRequirementsWorkspace,
    ),
  { loading: () => <p className="muted">Cargando necesidades…</p> },
);
const EmbeddedProjectsWorkspace = dynamic(
  () =>
    import("./ProjectsWorkspace").then(
      (module) => module.ProjectsWorkspace,
    ),
  { loading: () => <p className="muted">Cargando proyectos…</p> },
);

type AssistantEmbeddedWindowProps = {
  view: AssistantAppView;
  currentUser: User;
  onRequestClose: () => void;
};

export function AssistantEmbeddedWindow({
  view,
  currentUser,
  onRequestClose,
}: AssistantEmbeddedWindowProps) {
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const dialogRef = useRef<HTMLElement | null>(null);
  const titleId = `assistant-app-view-${view.id}`;

  useEffect(() => {
    closeButtonRef.current?.focus({ preventScroll: true });
    const previousBodyOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onRequestClose();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) {
        return;
      }
      const focusable = Array.from(
        dialogRef.current.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => element.getClientRects().length > 0);
      if (focusable.length === 0) {
        event.preventDefault();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      document.body.style.overflow = previousBodyOverflow;
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [onRequestClose, view.id]);

  function handleBackdropClick(event: MouseEvent<HTMLDivElement>) {
    if (event.target === event.currentTarget) {
      onRequestClose();
    }
  }

  return (
    <div
      className="assistant-embedded-backdrop"
      onMouseDown={handleBackdropClick}
      role="presentation"
    >
      <section
        aria-labelledby={titleId}
        aria-modal="true"
        className="assistant-embedded-window"
        ref={dialogRef}
        role="dialog"
      >
        <header className="assistant-embedded-header">
          <div>
            <p className="eyebrow">Vista de Anacleto</p>
            <h2 id={titleId}>{view.title}</h2>
          </div>
          <div className="assistant-embedded-controls">
            <a
              className="secondary-button assistant-embedded-external"
              href={buildAssistantAppViewHref(view)}
              rel="noreferrer"
              target="_blank"
            >
              <ExternalLink aria-hidden="true" size={16} />
              Abrir completa
            </a>
            <button
              aria-label="Cerrar ventana"
              className="secondary-button assistant-embedded-close"
              onClick={onRequestClose}
              ref={closeButtonRef}
              type="button"
            >
              <X aria-hidden="true" size={18} />
            </button>
          </div>
        </header>
        <div className="assistant-embedded-body">
          {view.surface === "map" ? (
            <div className="workspace map-workspace">
              <EmbeddedMapPanel
                embeddedContext={view.context}
                user={currentUser}
              />
            </div>
          ) : null}
          {view.surface === "requirements" ? (
            <EmbeddedRequirementsWorkspace context={view.context} />
          ) : null}
          {view.surface === "projects" ? (
            <EmbeddedProjectsWorkspace embeddedContext={view.context} />
          ) : null}
        </div>
      </section>
    </div>
  );
}
