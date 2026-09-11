"use client";

import { useEffect, useState, type FormEvent } from "react";
import { adminRequest, adminRequestWithTotal } from "../lib/api";
import type { MunicipalTask } from "./types";
import styles from "./AssetInventory.module.css";

type LinkOption = { id: number; name: string };

type TaskDetail = MunicipalTask & { events: Array<{ id: number; event_type: string; note: string | null; to_status: MunicipalTask["status"] | null; created_at: string }> };
const LABELS = { pending: "Pendiente", in_progress: "En curso", blocked: "Bloqueada", completed: "Completada", cancelled: "Cancelada" };
const TRANSITIONS: Record<MunicipalTask["status"], MunicipalTask["status"][]> = {
  pending: ["in_progress", "blocked", "completed", "cancelled"],
  in_progress: ["pending", "blocked", "completed", "cancelled"],
  blocked: ["pending", "in_progress", "cancelled"], completed: ["pending"], cancelled: ["pending"],
};
export function MunicipalTaskEditor({ taskId, organizationId, canEdit, canManage, onClose, onSaved }: {
  taskId: number | "new"; organizationId: number; canEdit: boolean; canManage: boolean;
  onClose: () => void; onSaved: () => void;
}) {
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [draft, setDraft] = useState({ title: "", description: "", priority: "normal", due_date: "", assignee_worker_id: "", project_id: "" });
  const [loading, setLoading] = useState(taskId !== "new");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [reason, setReason] = useState("");
  const [options, setOptions] = useState<{ workers: LinkOption[]; projects: LinkOption[] }>({ workers: [], projects: [] });
  const [optionsReady, setOptionsReady] = useState(false);
  const [optionsError, setOptionsError] = useState(false);
  useEffect(() => {
    if (!canEdit) return;
    const controller = new AbortController();
    setOptionsReady(false); setOptionsError(false);
    async function load(kind: "worker" | "project") {
      const result: LinkOption[] = [];
      for (let offset = 0; !controller.signal.aborted; offset += 200) {
        const page = await adminRequestWithTotal<LinkOption[]>(`/tasks/link-options?organization_id=${organizationId}&kind=${kind}&limit=200&offset=${offset}`, "", "No se pudieron cargar las opciones.", { signal: controller.signal });
        result.push(...page.items);
        if (!page.items.length || result.length >= page.total) break;
      }
      return result;
    }
    void Promise.all([load("worker"), load("project")]).then(([workers, projects]) => {
      if (!controller.signal.aborted) { setOptions({ workers, projects }); setOptionsReady(true); }
    }).catch(() => { if (!controller.signal.aborted) setOptionsError(true); });
    return () => controller.abort();
  }, [organizationId, canEdit]);
  useEffect(() => {
    if (taskId === "new") return;
    const controller = new AbortController();
    void adminRequest<TaskDetail>(`/tasks/${taskId}`, "", "No se pudo abrir la tarea.", { signal: controller.signal })
      .then((result) => {
        if (controller.signal.aborted) return;
        if (result.organization_id !== organizationId) throw new Error("La tarea pertenece a otra organización.");
        setTask(result);
        setDraft({ title: result.title, description: result.description || "", priority: result.priority, due_date: result.due_date || "", assignee_worker_id: result.assignee_worker_id?.toString() || "", project_id: result.project_id?.toString() || "" });
      }).catch(() => { if (!controller.signal.aborted) setError("No se pudo abrir esta tarea con tu acceso actual."); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [taskId, organizationId]);
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!canEdit || busy) return;
    setBusy(true); setError("");
    try {
      await adminRequest(`/tasks${taskId === "new" ? "" : `/${taskId}`}`, "", "No se pudo guardar la tarea.", {
        method: taskId === "new" ? "POST" : "PATCH",
        body: JSON.stringify({ title: draft.title, priority: draft.priority,
          ...(taskId === "new" || draft.assignee_worker_id !== (task?.assignee_worker_id?.toString() || "") ? { assignee_worker_id: draft.assignee_worker_id ? Number(draft.assignee_worker_id) : null } : {}),
          ...(taskId === "new" || draft.project_id !== (task?.project_id?.toString() || "") ? { project_id: draft.project_id ? Number(draft.project_id) : null } : {}),
          description: draft.description || null, due_date: draft.due_date || null,
          ...(taskId === "new" ? { organization_id: organizationId } : {}) }),
      });
      onSaved(); onClose();
    } catch { setError("No se pudo guardar. Revisa los datos y tus permisos antes de reintentar."); }
    finally { setBusy(false); }
  }
  async function transition(status: MunicipalTask["status"]) {
    if (!task || busy) return;
    const needsReason = ["blocked", "cancelled"].includes(status) || ["completed", "cancelled"].includes(task.status);
    if (needsReason && !reason.trim()) { setError("Indica el motivo de esta decisión."); return; }
    setBusy(true); setError("");
    try {
      const updated = await adminRequest<TaskDetail>(`/tasks/${task.id}/transition`, "", "No se pudo cambiar el estado.", {
        method: "POST", body: JSON.stringify({ status, reason: reason.trim() || null }),
      });
      setTask(updated); setReason(""); onSaved();
    } catch { setError("No se pudo cambiar el estado. La tarea o tus permisos pueden haber cambiado; vuelve a abrir la ficha."); }
    finally { setBusy(false); }
  }
  return <section className={styles.editor} aria-label="Ficha de la tarea">
    <div className={styles.editorHeading}><h2>{taskId === "new" ? "Nueva tarea" : "Ficha de la tarea"}</h2><button type="button" disabled={busy} onClick={onClose}>Cerrar ficha</button></div>
    {loading ? <p role="status">Cargando tarea…</p> : taskId === "new" || task ? <>
      {task ? <p>Estado: {LABELS[task.status]} · Responsable: {task.assignee?.full_name || "Sin asignar"}</p> : null}
      <form onSubmit={save}>
        <fieldset disabled={!canEdit || busy} className={styles.formGrid}>
          <label>Nombre<input required maxLength={255} value={draft.title} onChange={e => setDraft({ ...draft, title: e.target.value })} /></label>
          <label>Descripción<textarea value={draft.description} onChange={e => setDraft({ ...draft, description: e.target.value })} /></label>
          <label>Prioridad<select value={draft.priority} onChange={e => setDraft({ ...draft, priority: e.target.value })}><option value="low">Baja</option><option value="normal">Normal</option><option value="high">Alta</option><option value="urgent">Urgente</option></select></label>
          <label>Fecha límite<input type="date" value={draft.due_date} onChange={e => setDraft({ ...draft, due_date: e.target.value })} /></label>
          <label>Responsable<select disabled={!optionsReady} value={draft.assignee_worker_id} onChange={e => setDraft({ ...draft, assignee_worker_id: e.target.value })}>
            <option value="">Sin asignar</option>
            {draft.assignee_worker_id && !options.workers.some(option => String(option.id) === draft.assignee_worker_id) ? <option value={draft.assignee_worker_id}>{task?.assignee?.full_name || "Responsable actual"}</option> : null}
            {options.workers.map(option => <option key={option.id} value={option.id}>{option.name}</option>)}
          </select></label>
          <label>Proyecto<select disabled={!optionsReady} value={draft.project_id} onChange={e => setDraft({ ...draft, project_id: e.target.value })}>
            <option value="">Sin proyecto</option>
            {draft.project_id && !options.projects.some(option => String(option.id) === draft.project_id) ? <option value={draft.project_id}>{task?.project?.name || "Proyecto actual"}</option> : null}
            {options.projects.map(option => <option key={option.id} value={option.id}>{option.name}</option>)}
          </select></label>
          {optionsError ? <p role="status">No se pudieron cargar responsables y proyectos. Las relaciones actuales se conservan.</p> : null}
          {canEdit ? <button type="submit">{busy ? "Guardando…" : "Guardar tarea"}</button> : null}
        </fieldset>
      </form>
      {task && (canEdit || canManage) ? <div>
        <label>Motivo del cambio<textarea disabled={busy} value={reason} onChange={e => setReason(e.target.value)} /></label>
        <div className={styles.formActions}>{TRANSITIONS[task.status].filter(status =>
          status === "cancelled" || ["completed", "cancelled"].includes(task.status) ? canManage : canEdit
        ).map(status => <button type="button" key={status} disabled={busy} onClick={() => void transition(status)}>{status === "pending" && ["completed", "cancelled"].includes(task.status) ? "Reabrir" : LABELS[status]}</button>)}</div>
      </div> : null}
      {task?.events?.length ? <details><summary>Historial de la tarea</summary><ol>{task.events.map(event => <li key={event.id}>{new Date(event.created_at).toLocaleString("es-ES")} · {event.to_status ? LABELS[event.to_status] : "Ficha actualizada"}{event.note ? ` · ${event.note}` : ""}</li>)}</ol></details> : null}
    </> : null}
    {error ? <p role="alert">{error}</p> : null}
  </section>;
}
