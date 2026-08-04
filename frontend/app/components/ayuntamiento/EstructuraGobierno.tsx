"use client";

import { CircleAlert, Landmark, ShieldCheck } from "lucide-react";
import workspaceStyles from "../MunicipalWorkspace.module.css";
import type { GovernmentLevel, GovernmentMember } from "../types";
import type { MunicipalCollection } from "../../lib/municipalWorkspace";
import styles from "./EstructuraGobierno.module.css";
import { SectionShell } from "./SectionShell";
import {
  GOVERNMENT_LEVEL_LABELS,
  ResourceState,
  formatDate,
  getInitials,
} from "./shared";

// Orden protocolario de la corporación. El backend ya devuelve los cargos por
// `sort_order`, pero la pantalla los agrupa por nivel y el nivel tiene un orden
// propio que no depende de cómo se hayan numerado los cargos.
const LEVEL_ORDER: GovernmentLevel[] = [
  "alcaldia",
  "tenencia",
  "concejalia",
  "secretaria",
];

function formatTerm(member: GovernmentMember) {
  if (!member.term_start_date && !member.term_end_date) {
    return null;
  }
  const start = member.term_start_date
    ? formatDate(member.term_start_date)
    : "Sin fecha de inicio";
  const end = member.term_end_date
    ? formatDate(member.term_end_date)
    : "en curso";
  return `${start} — ${end}`;
}

export function EstructuraGobierno({
  members,
  error,
  canView,
  onRetry,
}: {
  members: MunicipalCollection<GovernmentMember> | null;
  error: string;
  canView: boolean;
  onRetry: () => void;
}) {
  const groups = LEVEL_ORDER.map((level) => ({
    level,
    members: (members?.items ?? []).filter((member) => member.level === level),
  })).filter((group) => group.members.length > 0);

  return (
    <SectionShell
      count={canView && !error ? (members?.total ?? null) : null}
      icon={Landmark}
      sectionKey="gobierno"
      title="Estructura de gobierno"
    >
      {!canView ? (
        <ResourceState
          description="Tu cuenta no dispone del permiso government.view en esta organización."
          icon={ShieldCheck}
          title="Corporación no autorizada"
          tone="restricted"
        />
      ) : error ? (
        <ResourceState
          action={
            <button
              className={workspaceStyles.secondaryAction}
              onClick={onRetry}
              type="button"
            >
              Reintentar
            </button>
          }
          description={error}
          icon={CircleAlert}
          title="Corporación no disponible"
          tone="error"
        />
      ) : groups.length > 0 ? (
        <div className={styles.levels}>
          {groups.map((group) => (
            <section className={styles.level} key={group.level}>
              <header className={styles.levelHeading}>
                <h3>{GOVERNMENT_LEVEL_LABELS[group.level]}</h3>
                <span>{group.members.length}</span>
              </header>
              <div className={styles.members}>
                {group.members.map((member) => {
                  const term = formatTerm(member);
                  return (
                    <article className={styles.member} key={member.id}>
                      <span
                        className={workspaceStyles.avatar}
                        aria-hidden="true"
                      >
                        {getInitials(member.full_name)}
                      </span>
                      <div>
                        <h4>{member.full_name}</h4>
                        <p className={styles.role}>{member.role_title}</p>
                        {member.political_group ? (
                          <span className={styles.group}>
                            {member.political_group}
                          </span>
                        ) : null}
                        <div className={styles.contact}>
                          {member.email ? (
                            <a href={`mailto:${member.email}`}>{member.email}</a>
                          ) : null}
                          {member.phone ? <span>{member.phone}</span> : null}
                        </div>
                        {term ? <p className={styles.term}>{term}</p> : null}
                      </div>
                    </article>
                  );
                })}
              </div>
            </section>
          ))}
        </div>
      ) : (
        <ResourceState
          description="Todavía no se ha registrado la alcaldía, las tenencias, las concejalías ni la secretaría. Los cargos archivados no se muestran aquí."
          icon={Landmark}
          title="Corporación sin registrar"
        />
      )}
    </SectionShell>
  );
}
