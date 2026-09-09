"use client";

import { CircleAlert, Landmark, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  SEDE_SECTIONS,
  countFor,
  fetchSedeContent,
  type SedeSectionKey,
} from "../lib/sede";
import { useSession } from "../lib/session";
import styles from "./RoadmapPanel.module.css";
import { ResourceState, formatDate, getMunicipalContexts } from "./ayuntamiento/shared";
import workspaceStyles from "./MunicipalWorkspace.module.css";
import type { SedeContent } from "./types";

const CHANNEL_LABELS: Record<string, string> = {
  in_person: "Presencial",
  online: "En línea",
  both: "Presencial y en línea",
};

const AREA_LABELS: Record<string, string> = {
  institutional: "Institucional",
  regulatory: "Normativa",
  economic: "Económica",
  contracts: "Contratos",
  grants: "Subvenciones",
  other: "Otra",
};

const BOARD_KIND_LABELS: Record<string, string> = {
  bando: "Bando",
  edicto: "Edicto",
  convocatoria: "Convocatoria",
  noticia: "Noticia",
  other: "Anuncio",
};

function EmptySection({ what }: { what: string }) {
  return <p className={styles.empty}>No hay {what} publicados ahora mismo.</p>;
}

/** Fila genérica: la sede es un listado, y todas sus secciones se leen igual. */
function Entry({
  title,
  meta,
  detail,
  badge,
}: {
  title: string;
  meta?: string | null;
  detail?: string | null;
  badge?: string | null;
}) {
  return (
    <li className={styles.task}>
      <div className={styles.taskHeading}>
        <h3>{title}</h3>
        {badge ? (
          <div className={styles.badges}>
            <span className={styles.badge}>{badge}</span>
          </div>
        ) : null}
      </div>
      {detail ? <p className={styles.taskDescription}>{detail}</p> : null}
      {meta ? (
        <div className={styles.taskMeta}>
          <span>{meta}</span>
        </div>
      ) : null}
    </li>
  );
}

export function SedePanel() {
  const { user, handleRequestError } = useSession();
  const searchParams = useSearchParams();
  const pathname = usePathname();
  const router = useRouter();
  const requested = searchParams.get("seccion");
  const activeSection: SedeSectionKey = SEDE_SECTIONS.some((section) => section.key === requested)
    ? (requested as SedeSectionKey) : "tablon";
  function setActiveSection(section: SedeSectionKey) {
    const params = new URLSearchParams(searchParams.toString());
    params.set("seccion", section);
    router.push(`${pathname}?${params}`, { scroll: false });
  }
  const [content, setContent] = useState<SedeContent | null>(null);
  const [error, setError] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [loadAttempt, setLoadAttempt] = useState(0);

  const contexts = user ? getMunicipalContexts(user) : [];
  const selectedContext = contexts[0] ?? null;
  const organizationId = selectedContext?.organization.id ?? null;
  const permissionSignature = (user?.permissions ?? []).slice().sort().join(",");

  useEffect(() => {
    if (organizationId === null) {
      setContent(null);
      return;
    }

    const controller = new AbortController();
    setIsLoading(true);
    setError("");

    fetchSedeContent(organizationId, controller.signal)
      .then((loaded) => {
        if (!controller.signal.aborted) {
          setContent(loaded);
        }
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          handleRequestError(
            reason,
            setError,
            "No se pudo cargar la sede electrónica.",
          );
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setIsLoading(false);
        }
      });

    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [organizationId, permissionSignature, loadAttempt]);

  if (!user) {
    return null;
  }
  if (!selectedContext) {
    return (
      <section className={`panel ${workspaceStyles.pageState}`}>
        <Landmark aria-hidden="true" size={28} strokeWidth={1.6} />
        <p className="eyebrow">Sede electrónica</p>
        <h1>Sin municipio afiliado</h1>
        <p className="muted">
          Tu cuenta no tiene ninguna organización municipal con municipio
          vinculado.
        </p>
      </section>
    );
  }

  return (
    <section className={styles.panel}>
      <header className={styles.header}>
        <div>
          <p>Sede electrónica</p>
          <h1>{selectedContext.municipality.name}</h1>
          <small>
            Lo que el ayuntamiento publica: tablón, trámites, tributos,
            contratación, transparencia, plenos y normativa.
          </small>
        </div>
      </header>

      <nav aria-label="Secciones de la sede" className={styles.tabs} role="tablist">
        {SEDE_SECTIONS.map((section) => {
          const count = countFor(content, section.key);
          return (
            <button
              aria-selected={activeSection === section.key}
              key={section.key}
              onClick={() => setActiveSection(section.key)}
              role="tab"
              type="button"
            >
              {section.label}
              {count !== null ? ` (${count})` : ""}
            </button>
          );
        })}
      </nav>

      {error ? (
        <ResourceState
          action={
            <button
              className={workspaceStyles.secondaryAction}
              onClick={() => setLoadAttempt((value) => value + 1)}
              type="button"
            >
              Reintentar
            </button>
          }
          description={error}
          icon={CircleAlert}
          title="Sede no disponible"
          tone="error"
        />
      ) : isLoading || content === null ? (
        <div
          aria-busy="true"
          aria-live="polite"
          className={workspaceStyles.loadingState}
          role="status"
        >
          <RefreshCw aria-hidden="true" size={22} />
          <div>
            <strong>Cargando la sede electrónica</strong>
            <span>Reuniendo lo publicado…</span>
          </div>
        </div>
      ) : (
        <ul className={styles.tasks}>
          {activeSection === "tablon" ? (
            content.board.length === 0 ? (
              <EmptySection what="anuncios" />
            ) : (
              content.board.map((entry) => (
                <Entry
                  badge={BOARD_KIND_LABELS[entry.kind] ?? entry.kind}
                  detail={entry.summary}
                  key={`${entry.kind}-${entry.id}`}
                  meta={
                    entry.published_on
                      ? `Publicado el ${formatDate(entry.published_on)}${
                          entry.expires_on
                            ? ` · hasta el ${formatDate(entry.expires_on)}`
                            : ""
                        }`
                      : null
                  }
                  title={entry.title}
                />
              ))
            )
          ) : null}

          {activeSection === "tramites" ? (
            content.procedures.length === 0 ? (
              <EmptySection what="trámites" />
            ) : (
              content.procedures.map((procedure) => (
                <Entry
                  badge={CHANNEL_LABELS[procedure.channel] ?? procedure.channel}
                  detail={procedure.description}
                  key={procedure.id}
                  meta={
                    procedure.deadline_days !== null
                      ? `Plazo de resolución: ${procedure.deadline_days} días`
                      : procedure.fee_description
                  }
                  title={procedure.name}
                />
              ))
            )
          ) : null}

          {activeSection === "tributos" ? (
            content.taxes.length === 0 ? (
              <EmptySection what="tributos" />
            ) : (
              content.taxes.map((tax) => (
                <Entry
                  badge={
                    tax.rate_value !== null
                      ? tax.rate_kind === "percentage"
                        ? `${tax.rate_value} %`
                        : tax.rate_value
                      : "Según tarifa"
                  }
                  detail={tax.rate_description}
                  key={tax.id}
                  meta={
                    tax.ordinance_id !== null
                      ? "Regulado por ordenanza fiscal"
                      : "Ordenanza fiscal no digitalizada"
                  }
                  title={tax.name}
                />
              ))
            )
          ) : null}

          {activeSection === "contratante" ? (
            content.contracts.length === 0 ? (
              <EmptySection what="contratos" />
            ) : (
              content.contracts.map((contract) => (
                <Entry
                  badge={contract.status}
                  key={contract.id}
                  meta={
                    contract.awarded_to
                      ? `Adjudicado a ${contract.awarded_to}`
                      : contract.reference
                  }
                  title={contract.title}
                />
              ))
            )
          ) : null}

          {activeSection === "transparencia" ? (
            content.transparency.length === 0 ? (
              <EmptySection what="elementos de transparencia" />
            ) : (
              content.transparency.map((item) => (
                <Entry
                  badge={AREA_LABELS[item.area] ?? item.area}
                  detail={item.description}
                  key={item.id}
                  meta={item.reference_period}
                  title={item.title}
                />
              ))
            )
          ) : null}

          {activeSection === "plenos" ? (
            content.sessions.length === 0 ? (
              <EmptySection what="plenos" />
            ) : (
              content.sessions.map((session) => (
                <Entry
                  badge={
                    session.minutes_status === "approved"
                      ? "Acta aprobada"
                      : "Acta pendiente"
                  }
                  detail={session.summary}
                  key={session.id}
                  meta={`Celebrado el ${formatDate(session.held_on)}`}
                  title={`Pleno ${session.kind}`}
                />
              ))
            )
          ) : null}

          {activeSection === "normativa" ? (
            content.ordinances.length === 0 ? (
              <EmptySection what="documentos normativos" />
            ) : (
              content.ordinances.map((ordinance) => (
                <Entry
                  badge={ordinance.topic}
                  key={ordinance.id}
                  meta={
                    ordinance.publication_date
                      ? `Publicada el ${formatDate(ordinance.publication_date)}`
                      : null
                  }
                  title={ordinance.title}
                />
              ))
            )
          ) : null}
        </ul>
      )}
    </section>
  );
}
