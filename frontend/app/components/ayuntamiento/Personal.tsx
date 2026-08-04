"use client";

import {
  CalendarDays,
  CircleAlert,
  ClipboardList,
  Clock,
  KeyRound,
  Receipt,
  ShieldCheck,
  UserRound,
  Users,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { fetchStaffWorkerDossier } from "../../lib/municipalWorkspace";
import type { MunicipalCollection } from "../../lib/municipalWorkspace";
import workspaceStyles from "../MunicipalWorkspace.module.css";
import type {
  Organization,
  StaffAbsence,
  StaffInvoice,
  StaffPost,
  StaffReport,
  StaffWorker,
} from "../types";
import styles from "./Personal.module.css";
import { SectionShell } from "./SectionShell";
import {
  STAFF_ABSENCE_TYPE_LABELS,
  STAFF_CONTRACT_TYPE_LABELS,
  STAFF_SCHEDULE_DAYS,
  STAFF_SCHEDULE_DAY_NAMES,
  STAFF_WORKER_STATUS_LABELS,
  ResourceState,
  SectionHeading,
  formatDate,
  getInitials,
} from "./shared";
import type { ResourceErrors } from "./types";

type Dossier = {
  absences: StaffAbsence[];
  reports: StaffReport[];
  invoices: StaffInvoice[];
};

const EMPTY_DOSSIER: Dossier = { absences: [], reports: [], invoices: [] };

/** Normaliza un rótulo para compararlo con el slug de `?puesto=` del menú. */
function slugify(value: string) {
  return value
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}

function formatWeeklyHours(value: string | null) {
  if (value === null) {
    return "No consta";
  }
  const hours = Number.parseFloat(value);
  if (!Number.isFinite(hours)) {
    return "No consta";
  }
  return `${new Intl.NumberFormat("es-ES", {
    maximumFractionDigits: 2,
  }).format(hours)} h/semana`;
}

function formatAmount(value: string | null) {
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
  }).format(amount);
}

function describeSchedule(worker: StaffWorker) {
  if (worker.schedule_summary) {
    return worker.schedule_summary;
  }
  if (worker.schedule_days.length === 0) {
    return "Sin horario registrado.";
  }
  const names = worker.schedule_days.map(
    (day) => STAFF_SCHEDULE_DAY_NAMES[day],
  );
  return `Atiende ${names.join(", ")}.`;
}

function Block({
  icon: Icon,
  title,
  count,
  children,
}: {
  icon: LucideIcon;
  title: string;
  count?: number;
  children: ReactNode;
}) {
  return (
    <section className={styles.block}>
      <header className={styles.blockHeading}>
        <h4>
          <Icon aria-hidden="true" size={15} strokeWidth={1.7} />
          {title}
        </h4>
        {typeof count === "number" ? <span>{count}</span> : null}
      </header>
      {children}
    </section>
  );
}

function WorkerDossier({
  worker,
  dossier,
  isLoading,
  error,
}: {
  worker: StaffWorker;
  dossier: Dossier;
  isLoading: boolean;
  error: string;
}) {
  const scheduledDays = new Set(worker.schedule_days);

  return (
    <article className={styles.dossier}>
      <header className={styles.dossierHeading}>
        <div>
          <h3>{worker.full_name}</h3>
          <p>{worker.post?.label ?? "Sin puesto asignado"}</p>
          <div className={styles.dossierContact}>
            {worker.email ? (
              <a href={`mailto:${worker.email}`}>{worker.email}</a>
            ) : null}
            {worker.phone ? <span>{worker.phone}</span> : null}
          </div>
        </div>
        <span
          className={workspaceStyles.statusPill}
          data-tone={worker.status === "active" ? "active" : "paused"}
        >
          {STAFF_WORKER_STATUS_LABELS[worker.status]}
        </span>
      </header>

      {worker.description ? (
        <p className={styles.scheduleSummary}>{worker.description}</p>
      ) : null}

      <Block icon={Clock} title="Horario semanal">
        <div className={styles.week}>
          {STAFF_SCHEDULE_DAYS.map(({ day, label }) => (
            <span
              aria-label={`${STAFF_SCHEDULE_DAY_NAMES[day]}: ${
                scheduledDays.has(day) ? "atiende" : "no atiende"
              }`}
              className={`${styles.day}${
                scheduledDays.has(day) ? ` ${styles.dayOn}` : ""
              }`}
              key={day}
            >
              {label}
            </span>
          ))}
        </div>
        <p className={styles.scheduleSummary}>{describeSchedule(worker)}</p>
      </Block>

      <Block icon={UserRound} title="Contrato">
        <dl className={styles.facts}>
          <div>
            <dt>Tipo</dt>
            <dd>
              {worker.contract_type
                ? STAFF_CONTRACT_TYPE_LABELS[worker.contract_type]
                : "No consta"}
            </dd>
          </div>
          <div>
            <dt>Jornada</dt>
            <dd>{formatWeeklyHours(worker.weekly_hours)}</dd>
          </div>
          <div>
            <dt>Alta</dt>
            <dd>
              {worker.contract_start_date
                ? formatDate(worker.contract_start_date)
                : "No consta"}
            </dd>
          </div>
          <div>
            <dt>Fin</dt>
            <dd>
              {worker.contract_end_date
                ? formatDate(worker.contract_end_date)
                : "Sin fecha de fin"}
            </dd>
          </div>
          <div>
            <dt>Vacaciones</dt>
            <dd>
              {worker.vacation_days_limit === null
                ? "No consta"
                : `${worker.vacation_days_limit} días`}
            </dd>
          </div>
          <div>
            <dt>Asuntos propios</dt>
            <dd>
              {worker.personal_days_limit === null
                ? "No consta"
                : `${worker.personal_days_limit} días`}
            </dd>
          </div>
        </dl>
      </Block>

      {error ? (
        <p className={styles.dossierNotice} role="alert">
          {error}
        </p>
      ) : isLoading ? (
        <p className={styles.dossierNotice} aria-live="polite">
          Cargando ausencias, diario y facturas…
        </p>
      ) : (
        <>
          <Block
            count={dossier.absences.length}
            icon={CalendarDays}
            title="Ausencias"
          >
            {dossier.absences.length > 0 ? (
              <ul className={styles.entries}>
                {dossier.absences.map((absence) => (
                  <li className={styles.entry} key={absence.id}>
                    <div className={styles.entryHeading}>
                      <strong>
                        {STAFF_ABSENCE_TYPE_LABELS[absence.absence_type]}
                      </strong>
                      <span className={styles.entryDate}>
                        {formatDate(absence.start_date)} —{" "}
                        {formatDate(absence.end_date)}
                      </span>
                    </div>
                    {absence.reason ? (
                      <div className={styles.entryBody}>
                        <p>{absence.reason}</p>
                      </div>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : (
              <p className={styles.emptyBlock}>
                No hay ausencias registradas para esta persona.
              </p>
            )}
          </Block>

          <Block
            count={dossier.reports.length}
            icon={ClipboardList}
            title="Diario y partes"
          >
            {dossier.reports.length > 0 ? (
              <ul className={styles.entries}>
                {dossier.reports.map((report) => (
                  <li className={styles.entry} key={report.id}>
                    <div className={styles.entryHeading}>
                      <strong>
                        {report.report_type === "diary"
                          ? "Entrada de diario"
                          : "Parte de trabajo"}
                      </strong>
                      <span className={styles.entryDate}>
                        {formatDate(report.report_date)}
                      </span>
                    </div>
                    <div className={styles.entryBody}>
                      {report.plan ? (
                        <p>
                          <span>Plan: </span>
                          {report.plan}
                        </p>
                      ) : null}
                      {report.closing ? (
                        <p>
                          <span>Cierre: </span>
                          {report.closing}
                        </p>
                      ) : null}
                      {report.incident ? (
                        <p>
                          <span>Incidencia: </span>
                          {report.incident}
                        </p>
                      ) : null}
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <p className={styles.emptyBlock}>
                Todavía no se ha anotado ningún día de trabajo.
              </p>
            )}
          </Block>

          {worker.bills_invoices ? (
            <Block
              count={dossier.invoices.length}
              icon={Receipt}
              title="Facturas"
            >
              {dossier.invoices.length > 0 ? (
                <ul className={styles.entries}>
                  {dossier.invoices.map((invoice) => {
                    const amount = formatAmount(invoice.amount);
                    return (
                      <li className={styles.entry} key={invoice.id}>
                        <div className={styles.entryHeading}>
                          <strong>{invoice.concept}</strong>
                          <span className={styles.entryDate}>
                            {formatDate(invoice.issued_on)}
                          </span>
                        </div>
                        <div className={styles.entryBody}>
                          <p>
                            {invoice.hours ? (
                              <>
                                <span>Horas: </span>
                                {invoice.hours}
                                {amount ? " · " : ""}
                              </>
                            ) : null}
                            {amount ? (
                              <span className={styles.amount}>{amount}</span>
                            ) : null}
                          </p>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              ) : (
                <p className={styles.emptyBlock}>
                  No hay facturas registradas para esta persona.
                </p>
              )}
            </Block>
          ) : null}
        </>
      )}
    </article>
  );
}

export function Personal({
  organization,
  workers,
  posts,
  errors,
  canViewStaff,
  onRetry,
}: {
  organization: Organization;
  workers: MunicipalCollection<StaffWorker> | null;
  posts: MunicipalCollection<StaffPost> | null;
  errors: ResourceErrors;
  canViewStaff: boolean;
  onRetry: () => void;
}) {
  const [selectedWorkerId, setSelectedWorkerId] = useState<number | null>(null);
  const [dossier, setDossier] = useState<Dossier>(EMPTY_DOSSIER);
  const [isDossierLoading, setIsDossierLoading] = useState(false);
  const [dossierError, setDossierError] = useState("");

  const items = useMemo(() => workers?.items ?? [], [workers]);

  // El menú superior enlaza cada puesto con `?puesto=`; si el rótulo coincide
  // con uno real de la plantilla, la ficha se abre directamente.
  const requestedPostSlug = useMemo(() => {
    if (typeof window === "undefined") {
      return null;
    }
    return new URLSearchParams(window.location.search).get("puesto");
  }, []);

  const selectedWorker =
    items.find((worker) => worker.id === selectedWorkerId) ?? null;

  useEffect(() => {
    if (items.length === 0) {
      setSelectedWorkerId(null);
      return;
    }
    setSelectedWorkerId((current) => {
      if (current !== null && items.some((worker) => worker.id === current)) {
        return current;
      }
      const requested = requestedPostSlug
        ? items.find(
            (worker) =>
              worker.post !== null &&
              slugify(worker.post.label) === requestedPostSlug,
          )
        : undefined;
      return (requested ?? items[0]).id;
    });
  }, [items, requestedPostSlug]);

  useEffect(() => {
    if (selectedWorker === null) {
      setDossier(EMPTY_DOSSIER);
      setDossierError("");
      setIsDossierLoading(false);
      return;
    }

    const controller = new AbortController();
    setDossier(EMPTY_DOSSIER);
    setDossierError("");
    setIsDossierLoading(true);

    fetchStaffWorkerDossier(selectedWorker.id, {
      includeInvoices: selectedWorker.bills_invoices,
      signal: controller.signal,
    })
      .then((loaded) => {
        if (!controller.signal.aborted) {
          setDossier(loaded);
        }
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        setDossierError(
          reason instanceof Error
            ? reason.message
            : "No se pudo cargar la ficha completa.",
        );
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setIsDossierLoading(false);
        }
      });

    return () => controller.abort();
    // La ficha se recarga sólo cuando cambia la persona o su forma de cobrar.
    // Depender del objeto entero volvería a pedir ausencias, diario y facturas
    // cada vez que la plantilla se refresca, aunque la persona sea la misma.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedWorker?.id, selectedWorker?.bills_invoices]);

  const postLabelById = useMemo(() => {
    const labels = new Map<number, string>();
    for (const post of posts?.items ?? []) {
      labels.set(post.id, post.label);
    }
    return labels;
  }, [posts]);

  const vacantPosts = (posts?.items ?? []).filter(
    (post) =>
      post.kind === "post" &&
      !items.some((worker) => worker.post_id === post.id),
  );

  return (
    <div className={workspaceStyles.tabContent}>
      <SectionHeading
        description="Plantilla municipal con su horario, ausencias, diario de trabajo y facturas. El directorio de acceso al sistema se muestra aparte."
        eyebrow="Organización"
        title="Personal del ayuntamiento"
      />

      <SectionShell
        count={canViewStaff && !errors.staff ? (workers?.total ?? null) : null}
        icon={Users}
        sectionKey="personal-plantilla"
        title="Plantilla municipal"
      >
        {!canViewStaff ? (
          <ResourceState
            description="Tu cuenta no dispone del permiso staff.view en esta organización."
            icon={ShieldCheck}
            title="Plantilla no autorizada"
            tone="restricted"
          />
        ) : errors.staff ? (
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
            description={errors.staff}
            icon={CircleAlert}
            title="Plantilla no disponible"
            tone="error"
          />
        ) : items.length > 0 ? (
          <div className={styles.layout}>
            <div className={styles.roster}>
              {items.map((worker) => (
                <button
                  aria-pressed={worker.id === selectedWorkerId}
                  className={`${styles.workerCard}${
                    worker.id === selectedWorkerId
                      ? ` ${styles.workerCardSelected}`
                      : ""
                  }`}
                  key={worker.id}
                  onClick={() => setSelectedWorkerId(worker.id)}
                  type="button"
                >
                  <span className={workspaceStyles.avatar} aria-hidden="true">
                    {getInitials(worker.full_name)}
                  </span>
                  <div>
                    <span className={styles.workerName}>
                      {worker.full_name}
                    </span>
                    <span className={styles.workerPost}>
                      {worker.post?.label ??
                        (worker.post_id !== null
                          ? (postLabelById.get(worker.post_id) ??
                            "Sin puesto asignado")
                          : "Sin puesto asignado")}
                    </span>
                    <span
                      className={styles.workerStatus}
                      data-tone={worker.status}
                    >
                      {STAFF_WORKER_STATUS_LABELS[worker.status]}
                    </span>
                  </div>
                </button>
              ))}
              {vacantPosts.length > 0 ? (
                <>
                  <p className={styles.groupLabel}>Puestos vacantes</p>
                  {vacantPosts.map((post) => (
                    <div className={styles.workerCard} key={post.id}>
                      <span
                        className={workspaceStyles.avatar}
                        aria-hidden="true"
                      >
                        —
                      </span>
                      <div>
                        <span className={styles.workerName}>{post.label}</span>
                        <span className={styles.workerPost}>
                          Sin persona asignada
                        </span>
                      </div>
                    </div>
                  ))}
                </>
              ) : null}
            </div>

            {selectedWorker ? (
              <WorkerDossier
                dossier={dossier}
                error={dossierError}
                isLoading={isDossierLoading}
                worker={selectedWorker}
              />
            ) : null}
          </div>
        ) : (
          <ResourceState
            description="Todavía no se ha registrado ninguna persona en la plantilla. Los puestos y las fichas se gestionan con los permisos staff.edit y staff.manage."
            icon={Users}
            title="Plantilla sin registrar"
          />
        )}
      </SectionShell>

      <SectionShell
        count={organization.users.length}
        defaultOpen={false}
        icon={KeyRound}
        sectionKey="personal-accesos"
        title="Directorio de acceso"
      >
        {organization.users.length > 0 ? (
          <div className={workspaceStyles.peopleGrid}>
            {organization.users.map((member) => (
              <article key={member.id}>
                <span className={workspaceStyles.avatar} aria-hidden="true">
                  {getInitials(member.full_name)}
                </span>
                <div>
                  <h3>{member.full_name}</h3>
                  <a href={`mailto:${member.email}`}>{member.email}</a>
                  <div className={workspaceStyles.itemMeta}>
                    <span>
                      {member.is_active ? "Acceso activo" : "Acceso inactivo"}
                    </span>
                    {member.is_superuser ? (
                      <span>Administración global</span>
                    ) : null}
                  </div>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <ResourceState
            description="Ninguna cuenta tiene acceso a esta organización en este momento. Pertenecer a la plantilla y tener acceso al sistema son cosas distintas."
            icon={Users}
            title="Sin cuentas asociadas"
          />
        )}
      </SectionShell>
    </div>
  );
}
