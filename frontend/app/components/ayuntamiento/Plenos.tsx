"use client";

import { Gavel, ShieldCheck } from "lucide-react";
import { SectionShell } from "./SectionShell";
import { ResourceState, formatDate } from "./shared";
import styles from "./Administracion.module.css";
import type { CouncilSession } from "../types";

const SESSION_KIND_LABELS: Record<CouncilSession["kind"], string> = {
  ordinary: "Ordinario",
  extraordinary: "Extraordinario",
  urgent: "Urgente",
  constitutive: "Constitutivo",
};

const SESSION_STATUS_LABELS: Record<CouncilSession["status"], string> = {
  convened: "Convocado",
  held: "Celebrado",
  cancelled: "Cancelado",
};

const MINUTES_STATUS_LABELS: Record<CouncilSession["minutes_status"], string> = {
  pending: "Acta pendiente",
  draft: "Acta en borrador",
  approved: "Acta aprobada",
};

/**
 * Un punto sin votación no es un punto con cero votos: los informativos
 * llegan con los tres campos nulos y no deben pintarse como si se hubieran
 * votado por unanimidad en contra.
 */
export function describeVote(item: CouncilSession["agenda_items"][number]) {
  if (
    item.votes_in_favour === null &&
    item.votes_against === null &&
    item.abstentions === null
  ) {
    return null;
  }
  const parts = [
    `${item.votes_in_favour ?? 0} a favor`,
    `${item.votes_against ?? 0} en contra`,
  ];
  if (item.abstentions) {
    parts.push(`${item.abstentions} abstenciones`);
  }
  return parts.join(" · ");
}

export function Plenos({
  sessions,
  canView,
}: {
  sessions: CouncilSession[];
  canView: boolean;
}) {
  return (
    <SectionShell
      count={canView ? sessions.length : null}
      icon={Gavel}
      sectionKey="plenos"
      title="Plenos municipales"
    >
      {!canView ? (
        <ResourceState
          description="Tu cuenta no dispone del permiso plenos.view en esta organización."
          icon={ShieldCheck}
          title="Plenos no autorizados"
          tone="restricted"
        />
      ) : sessions.length === 0 ? (
        <ResourceState
          description="Todavía no se ha convocado ninguna sesión del pleno."
          icon={Gavel}
          title="Sin sesiones registradas"
        />
      ) : (
        <div className={styles.blocks}>
          {sessions.map((session) => (
            <section className={styles.block} key={session.id}>
              <h3>
                <Gavel aria-hidden="true" size={15} strokeWidth={1.7} />
                Pleno {SESSION_KIND_LABELS[session.kind].toLowerCase()} ·{" "}
                {formatDate(session.held_on)}
              </h3>
              <div className={styles.badges}>
                <span className={styles.badge} data-tone={session.status}>
                  {SESSION_STATUS_LABELS[session.status]}
                </span>
                <span
                  className={styles.badge}
                  data-tone={
                    session.minutes_status === "approved" ? "granted" : undefined
                  }
                >
                  {MINUTES_STATUS_LABELS[session.minutes_status]}
                </span>
              </div>
              {session.agenda_items.length > 0 ? (
                <ol className={styles.agenda}>
                  {session.agenda_items.map((item) => {
                    const vote = describeVote(item);
                    return (
                      <li key={item.id}>
                        <strong>{item.title}</strong>
                        {vote ? <small>{vote}</small> : null}
                        {item.outcome ? (
                          <span className={styles.badge}>{item.outcome}</span>
                        ) : null}
                      </li>
                    );
                  })}
                </ol>
              ) : (
                <p className={styles.emptyAgenda}>Sin orden del día registrado.</p>
              )}
            </section>
          ))}
        </div>
      )}
    </SectionShell>
  );
}
