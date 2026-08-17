"use client";

import { Clock, FileText, HandCoins, ScrollText, ShieldCheck } from "lucide-react";
import { SectionShell } from "./SectionShell";
import { ResourceState, formatDate } from "./shared";
import styles from "./Administracion.module.css";
import type {
  MunicipalContract,
  MunicipalGrant,
  MunicipalLicence,
  OfficeHour,
  Weekday,
} from "../types";

const WEEKDAY_LABELS: Record<Weekday, string> = {
  monday: "Lunes",
  tuesday: "Martes",
  wednesday: "Miércoles",
  thursday: "Jueves",
  friday: "Viernes",
  saturday: "Sábado",
  sunday: "Domingo",
};

const LICENCE_STATUS_LABELS: Record<MunicipalLicence["status"], string> = {
  requested: "Solicitada",
  in_review: "En revisión",
  granted: "Concedida",
  denied: "Denegada",
  expired: "Caducada",
  withdrawn: "Desistida",
};

const LICENCE_KIND_LABELS: Record<MunicipalLicence["kind"], string> = {
  works: "Obras",
  opening: "Apertura",
  occupancy: "Ocupación",
  environmental: "Ambiental",
  other: "Otra",
};

const CONTRACT_STATUS_LABELS: Record<MunicipalContract["status"], string> = {
  draft: "Borrador",
  published: "Publicado",
  awarded: "Adjudicado",
  executed: "Ejecutado",
  cancelled: "Cancelado",
};

const GRANT_STATUS_LABELS: Record<MunicipalGrant["status"], string> = {
  open: "Convocatoria abierta",
  applied: "Solicitada",
  granted: "Concedida",
  denied: "Denegada",
  settled: "Justificada",
};

/** Los minutos desde medianoche del backend se leen como una hora. */
export function formatMinutes(minutes: number) {
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return `${String(hours).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

export function formatAmount(value: string | null) {
  if (value === null) {
    return null;
  }
  const amount = Number.parseFloat(value);
  if (!Number.isFinite(amount)) {
    return null;
  }
  return new Intl.NumberFormat("es-ES", {
    style: "currency",
    currency: "EUR",
    maximumFractionDigits: 0,
  }).format(amount);
}

function Restricted({ what }: { what: string }) {
  return (
    <ResourceState
      description={`Tu cuenta no dispone del permiso administration.view en esta organización.`}
      icon={ShieldCheck}
      title={`${what} no autorizado`}
      tone="restricted"
    />
  );
}

export function Administracion({
  officeHours,
  licences,
  contracts,
  grants,
  canView,
}: {
  officeHours: OfficeHour[];
  licences: MunicipalLicence[];
  contracts: MunicipalContract[];
  grants: MunicipalGrant[];
  canView: boolean;
}) {
  if (!canView) {
    return (
      <SectionShell
        icon={FileText}
        sectionKey="administracion"
        title="Administración municipal"
      >
        <Restricted what="Administración" />
      </SectionShell>
    );
  }

  const hasAnything =
    officeHours.length + licences.length + contracts.length + grants.length > 0;

  return (
    <SectionShell
      count={hasAnything ? licences.length + contracts.length + grants.length : null}
      icon={FileText}
      sectionKey="administracion"
      title="Administración municipal"
    >
      {!hasAnything ? (
        <ResourceState
          description="Todavía no se han registrado horarios de atención, licencias, contratos ni subvenciones."
          icon={FileText}
          title="Administración sin datos"
        />
      ) : (
        <div className={styles.blocks}>
          {officeHours.length > 0 ? (
            <section className={styles.block}>
              <h3>
                <Clock aria-hidden="true" size={15} strokeWidth={1.7} />
                Atención al público
              </h3>
              <ul className={styles.hours}>
                {officeHours.map((row) => (
                  <li key={row.id}>
                    <strong>{WEEKDAY_LABELS[row.weekday]}</strong>
                    <span>
                      {formatMinutes(row.opens_at)}–{formatMinutes(row.closes_at)}
                    </span>
                    <small>{row.office_name}</small>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          {licences.length > 0 ? (
            <section className={styles.block}>
              <h3>
                <ScrollText aria-hidden="true" size={15} strokeWidth={1.7} />
                Licencias
              </h3>
              <ul className={styles.rows}>
                {licences.map((licence) => (
                  <li key={licence.id}>
                    <div>
                      <strong>{licence.applicant}</strong>
                      <small>
                        {LICENCE_KIND_LABELS[licence.kind]} · {licence.reference}
                      </small>
                    </div>
                    <span className={styles.badge} data-tone={licence.status}>
                      {LICENCE_STATUS_LABELS[licence.status]}
                    </span>
                    <span className={styles.date}>
                      {formatDate(licence.requested_on)}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          {contracts.length > 0 ? (
            <section className={styles.block}>
              <h3>
                <FileText aria-hidden="true" size={15} strokeWidth={1.7} />
                Perfil de contratante
              </h3>
              <ul className={styles.rows}>
                {contracts.map((contract) => {
                  const amount = formatAmount(
                    contract.awarded_amount ?? contract.base_amount,
                  );
                  return (
                    <li key={contract.id}>
                      <div>
                        <strong>{contract.title}</strong>
                        <small>
                          {contract.reference}
                          {contract.awarded_to ? ` · ${contract.awarded_to}` : ""}
                        </small>
                      </div>
                      <span className={styles.badge} data-tone={contract.status}>
                        {CONTRACT_STATUS_LABELS[contract.status]}
                      </span>
                      <span className={styles.date}>{amount ?? "—"}</span>
                    </li>
                  );
                })}
              </ul>
            </section>
          ) : null}

          {grants.length > 0 ? (
            <section className={styles.block}>
              <h3>
                <HandCoins aria-hidden="true" size={15} strokeWidth={1.7} />
                Subvenciones
              </h3>
              <ul className={styles.rows}>
                {grants.map((grant) => (
                  <li key={grant.id}>
                    <div>
                      <strong>{grant.title}</strong>
                      <small>{grant.funder ?? "Sin financiador"}</small>
                    </div>
                    <span className={styles.badge} data-tone={grant.status}>
                      {GRANT_STATUS_LABELS[grant.status]}
                    </span>
                    <span className={styles.date}>
                      {grant.application_deadline
                        ? formatDate(grant.application_deadline)
                        : "Sin plazo"}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </div>
      )}
    </SectionShell>
  );
}
