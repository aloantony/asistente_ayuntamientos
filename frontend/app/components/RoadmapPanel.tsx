"use client";

import { CircleAlert, ClipboardList, RefreshCw, ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  ROADMAP_FILTERS,
  ROADMAP_PAGE_SIZE,
  fetchRoadmapGovernment,
  fetchTaskSummary,
  fetchTasks,
  type RoadmapFilterKey,
} from "../lib/roadmap";
import type { MunicipalCollection } from "../lib/municipalWorkspace";
import { canViewGovernment, canViewTasks } from "../lib/permissions";
import { useSession } from "../lib/session";
import styles from "./RoadmapPanel.module.css";
import { MunicipalTaskEditor } from "./MunicipalTaskEditor";
import { userHasPermission } from "./types";
import { EstructuraGobierno } from "./ayuntamiento/EstructuraGobierno";
import { ResourceState, formatDate, getMunicipalContexts } from "./ayuntamiento/shared";
import workspaceStyles from "./MunicipalWorkspace.module.css";
import type {
  GovernmentMember,
  MunicipalTask,
  MunicipalTaskSummary,
} from "./types";

const TASK_STATUS_LABELS: Record<MunicipalTask["status"], string> = {
  pending: "Pendiente",
  in_progress: "En curso",
  blocked: "Bloqueada",
  completed: "Completada",
  cancelled: "Cancelada",
};

const TASK_PRIORITY_LABELS: Record<MunicipalTask["priority"], string> = {
  low: "Prioridad baja",
  normal: "Prioridad normal",
  high: "Prioridad alta",
  urgent: "Urgente",
};

type RoadmapTab = "tareas" | "proyectos" | "corporacion";

const TABS: { id: RoadmapTab; label: string }[] = [
  { id: "tareas", label: "Tareas" },
  { id: "proyectos", label: "Proyectos" },
  { id: "corporacion", label: "Corporación" },
];

/**
 * Vencida se decide contra la fecha que el servidor ha usado para el resumen,
 * no contra el reloj del navegador: así la banda de filtros y las tarjetas
 * cuentan lo mismo aunque el equipo tenga la hora desviada.
 */
export function isOverdue(task: MunicipalTask, referenceDate: string | null) {
  if (!task.due_date || !referenceDate) {
    return false;
  }
  if (task.status === "completed" || task.status === "cancelled") {
    return false;
  }
  return task.due_date < referenceDate;
}

function taskTone(task: MunicipalTask, referenceDate: string | null) {
  if (task.status === "blocked") {
    return "blocked";
  }
  if (isOverdue(task, referenceDate)) {
    return "overdue";
  }
  return task.status;
}

function TaskCard({
  task,
  referenceDate,
  onOpen,
}: {
  task: MunicipalTask;
  referenceDate: string | null;
  onOpen: () => void;
}) {
  const overdue = isOverdue(task, referenceDate);

  return (
    <li className={styles.task} data-tone={taskTone(task, referenceDate)}>
      <div className={styles.taskHeading}>
        <h3><button type="button" onClick={onOpen}>{task.title}</button></h3>
        <div className={styles.badges}>
          {overdue ? (
            <span className={styles.badge} data-tone="overdue">
              Vencida
            </span>
          ) : null}
          <span
            className={styles.badge}
            data-tone={task.status === "blocked" ? "blocked" : undefined}
          >
            {TASK_STATUS_LABELS[task.status]}
          </span>
          {task.priority !== "normal" ? (
            <span className={styles.badge} data-tone={task.priority}>
              {TASK_PRIORITY_LABELS[task.priority]}
            </span>
          ) : null}
        </div>
      </div>

      {task.description ? (
        <p className={styles.taskDescription}>{task.description}</p>
      ) : null}
      {task.blocked_reason ? (
        <p className={styles.taskDescription}>
          <strong>Bloqueo: </strong>
          {task.blocked_reason}
        </p>
      ) : null}

      <div className={styles.taskMeta}>
        <span>
          {task.due_date ? (
            <>
              <strong>Límite:</strong> {formatDate(task.due_date)}
            </>
          ) : (
            "Sin fecha límite"
          )}
        </span>
        <span>
          {task.assignee ? (
            <>
              <strong>Responsable:</strong> {task.assignee.full_name}
            </>
          ) : (
            "Sin responsable"
          )}
        </span>
        {task.project ? (
          <span>
            <strong>Proyecto:</strong> {task.project.name}
          </span>
        ) : null}
      </div>
    </li>
  );
}

function groupBy(
  tasks: MunicipalTask[],
  key: (task: MunicipalTask) => string,
) {
  const groups = new Map<string, MunicipalTask[]>();
  for (const task of tasks) {
    const label = key(task);
    groups.set(label, [...(groups.get(label) ?? []), task]);
  }
  return [...groups.entries()].sort(([left], [right]) =>
    left.localeCompare(right, "es"),
  );
}

export function RoadmapPanel({ initialOrganizationId, initialTaskId }: { initialOrganizationId?: number | null; initialTaskId?: number | null } = {}) {
  const { user, handleRequestError } = useSession();
  const [editor, setEditor] = useState<number | "new" | null>(initialTaskId ?? null);
  const [activeTab, setActiveTab] = useState<RoadmapTab>("tareas");
  const [activeFilter, setActiveFilter] = useState<RoadmapFilterKey>("todas");
  const [page, setPage] = useState({ context: "", offset: 0 });
  const [summary, setSummary] = useState<MunicipalTaskSummary | null>(null);
  const [tasks, setTasks] = useState<MunicipalCollection<MunicipalTask> | null>(
    null,
  );
  const [government, setGovernment] = useState<
    MunicipalCollection<GovernmentMember> | null
  >(null);
  const [taskError, setTaskError] = useState("");
  const [governmentError, setGovernmentError] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [loadAttempt, setLoadAttempt] = useState(0);

  const contexts = user ? getMunicipalContexts(user) : [];
  const selectedContext = contexts.find(context => context.organization.id === initialOrganizationId) ?? contexts[0] ?? null;
  const organizationId = selectedContext?.organization.id ?? null;
  const canWrite = selectedContext?.organization.status === "active";
  const canManage = Boolean(user && canWrite && userHasPermission(user, "tasks.manage"));
  const canCreate = Boolean(user && canWrite && (canManage || userHasPermission(user, "tasks.create")));
  const canEdit = Boolean(user && canWrite && (canManage || userHasPermission(user, "tasks.edit")));
  const canSeeTasks = Boolean(user && canViewTasks(user));
  const canSeeGovernment = Boolean(user && canViewGovernment(user));
  const permissionSignature = (user?.permissions ?? []).slice().sort().join(",");

  const selectedFilter = useMemo(
    () =>
      ROADMAP_FILTERS.find((entry) => entry.key === activeFilter) ??
      ROADMAP_FILTERS[0],
    [activeFilter],
  );

  const pageContext = `${organizationId}:${permissionSignature}:${activeFilter}`;
  const offset = page.context === pageContext ? page.offset : 0;

  useEffect(() => {
    if (organizationId === null || !canSeeTasks) {
      setSummary(null);
      setTasks(null);
      setIsLoading(false);
      return;
    }

    const controller = new AbortController();
    setIsLoading(true);
    setTaskError("");

    Promise.allSettled([
      fetchTaskSummary(organizationId, controller.signal),
      fetchTasks(organizationId, selectedFilter.filter, controller.signal, offset),
    ])
      .then(([summaryResult, taskResult]) => {
        if (controller.signal.aborted) {
          return;
        }
        if (summaryResult.status === "fulfilled") {
          setSummary(summaryResult.value);
        }
        if (taskResult.status === "fulfilled") {
          setTasks(taskResult.value);
        } else {
          handleRequestError(
            taskResult.reason,
            setTaskError,
            "No se pudo cargar la hoja de ruta municipal.",
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
  }, [organizationId, canSeeTasks, selectedFilter.key, permissionSignature, loadAttempt, offset]);

  useEffect(() => {
    if (organizationId === null || !canSeeGovernment) {
      setGovernment(null);
      return;
    }

    const controller = new AbortController();
    setGovernmentError("");
    fetchRoadmapGovernment(organizationId, controller.signal)
      .then((loaded) => {
        if (!controller.signal.aborted) {
          setGovernment(loaded);
        }
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          handleRequestError(
            reason,
            setGovernmentError,
            "No se pudo cargar la corporación municipal.",
          );
        }
      });

    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [organizationId, canSeeGovernment, permissionSignature, loadAttempt]);

  if (!user) {
    return null;
  }
  if (!selectedContext) {
    return (
      <section className={`panel ${workspaceStyles.pageState}`}>
        <ClipboardList aria-hidden="true" size={28} strokeWidth={1.6} />
        <p className="eyebrow">Hoja de ruta</p>
        <h1>Sin municipio afiliado</h1>
        <p className="muted">
          Tu cuenta no tiene ninguna organización municipal con municipio
          vinculado.
        </p>
      </section>
    );
  }

  const items = tasks?.items ?? [];
  const referenceDate = summary?.reference_date ?? null;
  const needsAttention = (summary?.blocked ?? 0) + (summary?.overdue ?? 0);
  const counts: Record<RoadmapFilterKey, number | null> = {
    todas: summary?.total ?? null,
    in_progress: summary?.in_progress ?? null,
    blocked: summary?.blocked ?? null,
    overdue: summary?.overdue ?? null,
    unassigned: summary?.unassigned ?? null,
    completed: summary?.completed ?? null,
  };

  return (
    <section className={styles.panel}>
      <header className={styles.header}>
        <div>
          <p>Hoja de ruta</p>
          <h1>{selectedContext.municipality.name}</h1>
          <small>
            Trabajo municipal en curso, con su responsable y su fecha límite.
          </small>
        </div>
        {canSeeTasks && canCreate ? <button type="button" onClick={() => setEditor("new")}>Nueva tarea</button> : null}
      </header>
      {editor !== null && organizationId !== null && canSeeTasks && (editor !== "new" || canCreate) ? <MunicipalTaskEditor
        key={`${user?.id}:${organizationId}:${editor}:${permissionSignature}`} taskId={editor} organizationId={organizationId}
        canEdit={editor === "new" ? canCreate : canEdit} canManage={canManage}
        onClose={() => setEditor(null)} onSaved={() => setLoadAttempt(value => value + 1)} /> : null}

      {canSeeTasks && needsAttention > 0 ? (
        <div className={styles.attention} role="status">
          <CircleAlert aria-hidden="true" size={19} strokeWidth={1.8} />
          <div>
            <strong>Atención requerida</strong>
            <span>
              {summary?.blocked ?? 0} bloqueada
              {(summary?.blocked ?? 0) === 1 ? "" : "s"} y{" "}
              {summary?.overdue ?? 0} vencida
              {(summary?.overdue ?? 0) === 1 ? "" : "s"} esperan una decisión.
            </span>
          </div>
        </div>
      ) : null}

      <nav aria-label="Áreas de la hoja de ruta" className={styles.tabs} role="tablist">
        {TABS.map((tab) => (
          <button
            aria-selected={activeTab === tab.id}
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            role="tab"
            type="button"
          >
            {tab.label}
          </button>
        ))}
      </nav>

      {activeTab === "corporacion" ? (
        <EstructuraGobierno
          canView={canSeeGovernment}
          error={governmentError}
          members={government}
          onRetry={() => setLoadAttempt((value) => value + 1)}
        />
      ) : !canSeeTasks ? (
        <ResourceState
          description="Tu cuenta no dispone del permiso tasks.view en esta organización."
          icon={ShieldCheck}
          title="Hoja de ruta no autorizada"
          tone="restricted"
        />
      ) : taskError ? (
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
          description={taskError}
          icon={CircleAlert}
          title="Hoja de ruta no disponible"
          tone="error"
        />
      ) : (
        <>
          <div className={styles.chips}>
            {ROADMAP_FILTERS.map((entry) => (
              <button
                aria-pressed={activeFilter === entry.key}
                className={styles.chip}
                data-tone={entry.key}
                key={entry.key}
                onClick={() => setActiveFilter(entry.key)}
                type="button"
              >
                {entry.label}
                {counts[entry.key] !== null ? (
                  <span className={styles.chipCount}>{counts[entry.key]}</span>
                ) : null}
              </button>
            ))}
          </div>

          {tasks && tasks.total > ROADMAP_PAGE_SIZE ? (
            <nav aria-label="Páginas de tareas" className={styles.chips}>
              <button type="button" disabled={isLoading || offset === 0}
                onClick={() => setPage({ context: pageContext, offset: Math.max(0, offset - ROADMAP_PAGE_SIZE) })}>
                Anterior
              </button>
              <span aria-live="polite">{offset + 1}–{Math.min(offset + ROADMAP_PAGE_SIZE, tasks.total)} de {tasks.total}</span>
              <button type="button" disabled={isLoading || offset + ROADMAP_PAGE_SIZE >= tasks.total}
                onClick={() => setPage({ context: pageContext, offset: offset + ROADMAP_PAGE_SIZE })}>
                Siguiente
              </button>
            </nav>
          ) : null}

          {isLoading ? (
            <div
              aria-busy="true"
              aria-live="polite"
              className={workspaceStyles.loadingState}
              role="status"
            >
              <RefreshCw aria-hidden="true" size={22} />
              <div>
                <strong>Cargando la hoja de ruta</strong>
                <span>Consultando el trabajo municipal…</span>
              </div>
            </div>
          ) : items.length === 0 ? (
            <p className={styles.empty}>
              No hay tareas que cumplan este filtro.
            </p>
          ) : activeTab === "proyectos" ? (
            groupBy(items, (task) => task.project?.name ?? "Sin proyecto").map(
              ([label, grouped]) => (
                <section className={styles.group} key={label}>
                  <header className={styles.groupHeading}>
                    <h2>{label}</h2>
                    <span>{grouped.length}</span>
                  </header>
                  <ul className={styles.tasks}>
                    {grouped.map((task) => (
                      <TaskCard
                        key={task.id}
                        referenceDate={referenceDate}
                        task={task}
                        onOpen={() => setEditor(task.id)}
                      />
                    ))}
                  </ul>
                </section>
              ),
            )
          ) : (
            <ul className={styles.tasks}>
              {items.map((task) => (
                <TaskCard
                  key={task.id}
                  referenceDate={referenceDate}
                  task={task}
                        onOpen={() => setEditor(task.id)}
                />
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
