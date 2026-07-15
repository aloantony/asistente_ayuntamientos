"use client";

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { ApiRequestError } from "../lib/api";
import {
  createMaintenanceOrder,
  fetchMaintenanceOrder,
  fetchMaintenanceOrders,
  transitionMaintenanceOrder,
} from "../lib/maintenance";
import { useSession } from "../lib/session";
import type {
  AssetStatus,
  MaintenanceOrder,
  MaintenanceOrderDetail,
  MaintenanceOrderPriority,
  MaintenanceOrderStatus,
  MaintenanceType,
  OrganizationStatus,
  User,
} from "./types";
import { userHasPermission } from "./types";

type AssetMaintenancePanelProps = {
  assetId: number;
  assetName: string;
  assetStatus: AssetStatus;
  organizationId: number;
  organizationStatus: OrganizationStatus;
  user: User;
};

type MaintenanceDraft = {
  title: string;
  description: string;
  maintenanceType: MaintenanceType;
  priority: MaintenanceOrderPriority;
  scheduledFor: string;
  estimatedMinutes: string;
};

type ReasonedTransitionDraft = {
  orderId: number;
  status: "cancelled" | "planned";
  note: string;
  successMessage: string;
};

const STATUS_LABELS: Record<MaintenanceOrderStatus, string> = {
  planned: "Planificada",
  scheduled: "Programada",
  in_progress: "En curso",
  completed: "Completada",
  cancelled: "Cancelada",
};

const PRIORITY_LABELS: Record<MaintenanceOrderPriority, string> = {
  low: "Baja",
  normal: "Normal",
  high: "Alta",
  urgent: "Urgente",
};

const TYPE_LABELS: Record<MaintenanceType, string> = {
  preventive: "Preventivo",
  corrective: "Correctivo",
  inspection: "Inspección",
  cleaning: "Limpieza",
  other: "Otro",
};

const CLOSED_STATUSES = new Set<MaintenanceOrderStatus>([
  "completed",
  "cancelled",
]);

function localDateKey(date = new Date()) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function emptyDraft(): MaintenanceDraft {
  return {
    title: "",
    description: "",
    maintenanceType: "preventive",
    priority: "normal",
    scheduledFor: localDateKey(),
    estimatedMinutes: "",
  };
}

function isAbortError(error: unknown) {
  return error instanceof Error && error.name === "AbortError";
}

function formatDate(value: string | null) {
  if (!value) {
    return "Sin fecha";
  }
  const date = new Date(`${value}T12:00:00`);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("es-ES", {
    day: "numeric",
    month: "short",
    year: "numeric",
  }).format(date);
}

function formatDateTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("es-ES", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function orderSortValue(order: MaintenanceOrder) {
  return order.scheduled_for ?? "9999-12-31";
}

function eventLabel(event: MaintenanceOrderDetail["events"][number]) {
  if (event.event_type === "created") {
    return "Orden creada";
  }
  if (event.event_type === "transition" && event.to_status) {
    const from = event.from_status ? STATUS_LABELS[event.from_status] : null;
    const to = STATUS_LABELS[event.to_status];
    return from ? `${from} → ${to}` : `Estado: ${to}`;
  }
  return "Datos actualizados";
}

export function AssetMaintenancePanel({
  assetId,
  assetName,
  assetStatus,
  organizationId,
  organizationStatus,
  user,
}: AssetMaintenancePanelProps) {
  const { getStoredToken, handleRequestError } = useSession();
  const [orders, setOrders] = useState<MaintenanceOrder[]>([]);
  const [total, setTotal] = useState(0);
  const [isLoading, setIsLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [statusMessage, setStatusMessage] = useState("");
  const [reloadVersion, setReloadVersion] = useState(0);
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const [draft, setDraft] = useState<MaintenanceDraft>(emptyDraft);
  const [isCreating, setIsCreating] = useState(false);
  const [formError, setFormError] = useState("");
  const [transitioningOrderId, setTransitioningOrderId] = useState<number | null>(
    null,
  );
  const [detail, setDetail] = useState<MaintenanceOrderDetail | null>(null);
  const [detailOrderId, setDetailOrderId] = useState<number | null>(null);
  const [isLoadingDetail, setIsLoadingDetail] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [reasonedTransition, setReasonedTransition] =
    useState<ReasonedTransitionDraft | null>(null);
  const listAbortRef = useRef<AbortController | null>(null);
  const detailAbortRef = useRef<AbortController | null>(null);
  const mutationAbortRef = useRef<AbortController | null>(null);
  const listSequenceRef = useRef(0);
  const detailSequenceRef = useRef(0);
  const mutationSequenceRef = useRef(0);
  const handleRequestErrorRef = useRef(handleRequestError);
  const triggerButtonRef = useRef<HTMLButtonElement | null>(null);
  const dialogRef = useRef<HTMLFormElement | null>(null);
  const titleInputRef = useRef<HTMLInputElement | null>(null);
  const transitionNoteRef = useRef<HTMLTextAreaElement | null>(null);
  const reasonTriggerButtonRef = useRef<HTMLButtonElement | null>(null);
  const isCreatingRef = useRef(false);

  const canViewAssets =
    userHasPermission(user, "assets.view") ||
    userHasPermission(user, "assets.manage");
  const canView =
    canViewAssets &&
    (userHasPermission(user, "maintenance.view") ||
      userHasPermission(user, "maintenance.manage"));
  const canWriteContext =
    organizationStatus === "active" &&
    assetStatus !== "retired" &&
    assetStatus !== "archived";
  const canCreate =
    canWriteContext &&
    canViewAssets &&
    (userHasPermission(user, "maintenance.create") ||
      userHasPermission(user, "maintenance.manage"));
  const canEdit =
    canWriteContext &&
    canViewAssets &&
    (userHasPermission(user, "maintenance.edit") ||
      userHasPermission(user, "maintenance.manage"));
  const canComplete =
    canWriteContext &&
    canViewAssets &&
    (userHasPermission(user, "maintenance.complete") ||
      userHasPermission(user, "maintenance.manage"));
  const canManage =
    canWriteContext &&
    canViewAssets &&
    userHasPermission(user, "maintenance.manage");
  const reasonedTransitionKey = reasonedTransition
    ? `${reasonedTransition.orderId}-${reasonedTransition.status}`
    : "";

  useEffect(() => {
    handleRequestErrorRef.current = handleRequestError;
  }, [handleRequestError]);

  useEffect(() => {
    isCreatingRef.current = isCreating;
  }, [isCreating]);

  useEffect(() => {
    setDetail(null);
    setDetailOrderId(null);
    setDetailError("");
    setStatusMessage("");
    setReasonedTransition(null);
  }, [assetId, organizationId]);

  useEffect(() => {
    if (!canView) {
      listAbortRef.current?.abort();
      listAbortRef.current = null;
      listSequenceRef.current += 1;
      setOrders([]);
      setTotal(0);
      setIsLoading(false);
      return;
    }

    listAbortRef.current?.abort();
    const controller = new AbortController();
    listAbortRef.current = controller;
    const requestSequence = ++listSequenceRef.current;
    setIsLoading(true);
    setLoadError("");

    void fetchMaintenanceOrders(
      {
        organizationId,
        assetId,
        includeClosed: true,
        limit: 100,
      },
      controller.signal,
    )
      .then(({ items, total: responseTotal }) => {
        if (
          controller.signal.aborted ||
          requestSequence !== listSequenceRef.current
        ) {
          return;
        }
        setOrders(items);
        setTotal(responseTotal);
      })
      .catch((error) => {
        if (
          controller.signal.aborted ||
          requestSequence !== listSequenceRef.current ||
          isAbortError(error)
        ) {
          return;
        }
        if (error instanceof ApiRequestError && error.status === 403) {
          setLoadError(
            "No puedes consultar el mantenimiento de este activo. Solicita permisos de mantenimiento e inventario para esta organización.",
          );
          return;
        }
        handleRequestErrorRef.current(
          error,
          setLoadError,
          "No se pudieron cargar las órdenes de mantenimiento.",
        );
      })
      .finally(() => {
        if (
          !controller.signal.aborted &&
          requestSequence === listSequenceRef.current
        ) {
          setIsLoading(false);
        }
      });

    return () => {
      controller.abort();
      if (listAbortRef.current === controller) {
        listAbortRef.current = null;
      }
    };
  }, [assetId, canView, organizationId, reloadVersion]);

  useEffect(() => {
    return () => {
      detailAbortRef.current?.abort();
      mutationAbortRef.current?.abort();
      detailSequenceRef.current += 1;
      mutationSequenceRef.current += 1;
    };
  }, []);

  useEffect(() => {
    if (!isDialogOpen) {
      return;
    }

    const triggerButton = triggerButtonRef.current;
    window.requestAnimationFrame(() => titleInputRef.current?.focus());

    function handleDialogKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape" && !isCreatingRef.current) {
        setIsDialogOpen(false);
        setFormError("");
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) {
        return;
      }
      const focusable = Array.from(
        dialogRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      );
      if (focusable.length === 0) {
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

    window.addEventListener("keydown", handleDialogKeyDown);
    return () => {
      window.removeEventListener("keydown", handleDialogKeyDown);
      window.requestAnimationFrame(() => triggerButton?.focus());
    };
  }, [isDialogOpen]);

  useEffect(() => {
    if (!reasonedTransitionKey) {
      return;
    }
    window.requestAnimationFrame(() => transitionNoteRef.current?.focus());
  }, [reasonedTransitionKey]);

  const groupedOrders = useMemo(() => {
    const today = localDateKey();
    const overdue: MaintenanceOrder[] = [];
    const upcoming: MaintenanceOrder[] = [];
    const history: MaintenanceOrder[] = [];

    for (const order of orders) {
      if (CLOSED_STATUSES.has(order.status)) {
        history.push(order);
      } else if (order.scheduled_for && order.scheduled_for < today) {
        overdue.push(order);
      } else {
        upcoming.push(order);
      }
    }

    overdue.sort((left, right) =>
      orderSortValue(left).localeCompare(orderSortValue(right)),
    );
    upcoming.sort((left, right) =>
      orderSortValue(left).localeCompare(orderSortValue(right)),
    );
    history.sort((left, right) => right.updated_at.localeCompare(left.updated_at));

    return { overdue, upcoming, history };
  }, [orders]);

  function openDialog() {
    setDraft(emptyDraft());
    setFormError("");
    setStatusMessage("");
    setIsDialogOpen(true);
  }

  function closeDialog() {
    if (isCreatingRef.current) {
      return;
    }
    setIsDialogOpen(false);
    setFormError("");
  }

  function closeReasonedTransition() {
    const triggerButton = reasonTriggerButtonRef.current;
    setReasonedTransition(null);
    window.requestAnimationFrame(() => triggerButton?.focus());
  }

  async function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canCreate) {
      return;
    }

    const title = draft.title.trim();
    if (!title) {
      setFormError("Escribe un título para el trabajo de mantenimiento.");
      titleInputRef.current?.focus();
      return;
    }
    if (!draft.scheduledFor) {
      setFormError("Selecciona la fecha prevista del mantenimiento.");
      return;
    }
    const estimatedMinutes = draft.estimatedMinutes.trim()
      ? Number(draft.estimatedMinutes)
      : null;
    if (
      estimatedMinutes !== null &&
      (!Number.isInteger(estimatedMinutes) || estimatedMinutes <= 0)
    ) {
      setFormError("La duración estimada debe ser un número entero positivo.");
      return;
    }

    mutationAbortRef.current?.abort();
    const controller = new AbortController();
    mutationAbortRef.current = controller;
    const requestSequence = ++mutationSequenceRef.current;
    setIsCreating(true);
    setFormError("");

    try {
      await createMaintenanceOrder(
        getStoredToken(),
        {
          asset_id: assetId,
          title,
          description: draft.description.trim() || null,
          maintenance_type: draft.maintenanceType,
          priority: draft.priority,
          scheduled_for: draft.scheduledFor,
          estimated_minutes: estimatedMinutes,
        },
        controller.signal,
      );
      if (
        controller.signal.aborted ||
        requestSequence !== mutationSequenceRef.current
      ) {
        return;
      }
      setIsDialogOpen(false);
      setStatusMessage("Mantenimiento programado correctamente.");
      listAbortRef.current?.abort();
      listAbortRef.current = null;
      listSequenceRef.current += 1;
      setIsLoading(false);
      setReloadVersion((current) => current + 1);
    } catch (error) {
      if (
        controller.signal.aborted ||
        requestSequence !== mutationSequenceRef.current ||
        isAbortError(error)
      ) {
        return;
      }
      if (error instanceof ApiRequestError && error.status === 403) {
        setFormError(
          "No puedes programar mantenimiento para este activo. Solicita permisos de creación y acceso al inventario de esta organización.",
        );
      } else if (error instanceof ApiRequestError && error.status === 409) {
        setFormError(
          "El activo ya no admite nuevas órdenes o cambió mientras se guardaba. Cierra, actualiza la ficha y vuelve a intentarlo.",
        );
      } else {
        handleRequestErrorRef.current(
          error,
          setFormError,
          "No se pudo programar el mantenimiento.",
        );
      }
    } finally {
      if (requestSequence === mutationSequenceRef.current) {
        setIsCreating(false);
        if (mutationAbortRef.current === controller) {
          mutationAbortRef.current = null;
        }
      }
    }
  }

  async function loadDetail(orderId: number) {
    if (detailOrderId === orderId && detail) {
      setDetail(null);
      setDetailOrderId(null);
      setDetailError("");
      return;
    }

    detailAbortRef.current?.abort();
    const controller = new AbortController();
    detailAbortRef.current = controller;
    const requestSequence = ++detailSequenceRef.current;
    setDetail(null);
    setDetailOrderId(orderId);
    setDetailError("");
    setIsLoadingDetail(true);

    try {
      const response = await fetchMaintenanceOrder(orderId, controller.signal);
      if (
        controller.signal.aborted ||
        requestSequence !== detailSequenceRef.current
      ) {
        return;
      }
      setDetail(response);
    } catch (error) {
      if (
        controller.signal.aborted ||
        requestSequence !== detailSequenceRef.current ||
        isAbortError(error)
      ) {
        return;
      }
      if (error instanceof ApiRequestError && error.status === 403) {
        setDetailError(
          "No puedes consultar el historial de esta orden en la organización seleccionada.",
        );
      } else {
        handleRequestErrorRef.current(
          error,
          setDetailError,
          "No se pudo cargar el historial de mantenimiento.",
        );
      }
    } finally {
      if (
        !controller.signal.aborted &&
        requestSequence === detailSequenceRef.current
      ) {
        setIsLoadingDetail(false);
        if (detailAbortRef.current === controller) {
          detailAbortRef.current = null;
        }
      }
    }
  }

  async function transitionOrder(
    order: MaintenanceOrder,
    nextStatus: MaintenanceOrderStatus,
    successMessage: string,
    note?: string,
  ) {
    mutationAbortRef.current?.abort();
    const controller = new AbortController();
    mutationAbortRef.current = controller;
    const requestSequence = ++mutationSequenceRef.current;
    setTransitioningOrderId(order.id);
    setStatusMessage("");
    setLoadError("");

    try {
      const response = await transitionMaintenanceOrder(
        getStoredToken(),
        order.id,
        { status: nextStatus, note },
        controller.signal,
      );
      if (
        controller.signal.aborted ||
        requestSequence !== mutationSequenceRef.current
      ) {
        return false;
      }
      listAbortRef.current?.abort();
      listAbortRef.current = null;
      listSequenceRef.current += 1;
      setIsLoading(false);
      if (detailOrderId === response.id) {
        detailAbortRef.current?.abort();
        detailAbortRef.current = null;
        detailSequenceRef.current += 1;
        setIsLoadingDetail(false);
      }
      setOrders((current) =>
        current.map((item) => (item.id === response.id ? response : item)),
      );
      if (detailOrderId === response.id) {
        setDetail(response);
      }
      setStatusMessage(successMessage);
      return true;
    } catch (error) {
      if (
        controller.signal.aborted ||
        requestSequence !== mutationSequenceRef.current ||
        isAbortError(error)
      ) {
        return false;
      }
      if (error instanceof ApiRequestError && error.status === 403) {
        setLoadError(
          nextStatus === "completed"
            ? "No puedes completar esta orden. Solicita el permiso específico de finalización y acceso al inventario."
            : nextStatus === "cancelled" || note
              ? "No puedes cancelar o reabrir esta orden. Estas acciones requieren gestión de mantenimiento y acceso al inventario."
              : "No puedes cambiar esta orden. Solicita permisos de edición y acceso al inventario para esta organización.",
        );
      } else if (error instanceof ApiRequestError && error.status === 409) {
        setLoadError(
          "La orden cambió mientras trabajabas o esa transición ya no es válida. Actualiza la lista y vuelve a intentarlo.",
        );
      } else if (error instanceof ApiRequestError && error.status === 422) {
        setLoadError(
          nextStatus === "in_progress" || nextStatus === "completed"
            ? "El responsable asignado ya no es válido para esta organización. Revisa la asignación en la gestión de mantenimiento y vuelve a intentarlo."
            : "La orden ya no cumple los requisitos para ese cambio de estado. Actualiza la lista y revisa sus datos.",
        );
      } else {
        handleRequestErrorRef.current(
          error,
          setLoadError,
          "No se pudo cambiar el estado de la orden.",
        );
      }
      return false;
    } finally {
      if (requestSequence === mutationSequenceRef.current) {
        setTransitioningOrderId(null);
        if (mutationAbortRef.current === controller) {
          mutationAbortRef.current = null;
        }
      }
    }
  }

  async function handleReasonedTransition(
    event: FormEvent<HTMLFormElement>,
    order: MaintenanceOrder,
  ) {
    event.preventDefault();
    if (!reasonedTransition || reasonedTransition.orderId !== order.id) {
      return;
    }
    const note = reasonedTransition.note.trim();
    if (!note) {
      setLoadError("Escribe un motivo antes de confirmar este cambio.");
      transitionNoteRef.current?.focus();
      return;
    }
    if (note.length > 2000) {
      setLoadError("El motivo no puede superar los 2.000 caracteres.");
      transitionNoteRef.current?.focus();
      return;
    }

    const succeeded = await transitionOrder(
      order,
      reasonedTransition.status,
      reasonedTransition.successMessage,
      note,
    );
    if (succeeded) {
      setReasonedTransition(null);
    }
  }

  function renderOrder(order: MaintenanceOrder, overdue = false) {
    const isTransitioning = transitioningOrderId === order.id;
    const isOpen = !CLOSED_STATUSES.has(order.status);
    const canStart =
      canEdit && (order.status === "planned" || order.status === "scheduled");
    const canFinish = canComplete && order.status === "in_progress";
    const canReopen = canManage && CLOSED_STATUSES.has(order.status);

    return (
      <article
        className={`maintenance-order-card${overdue ? " maintenance-order-card--overdue" : ""}`}
        key={order.id}
      >
        <div className="maintenance-order-heading">
          <div>
            <strong>{order.title}</strong>
            <span>
              {TYPE_LABELS[order.maintenance_type]} · {formatDate(order.scheduled_for)}
            </span>
          </div>
          <span className={`maintenance-status maintenance-status--${order.status}`}>
            {STATUS_LABELS[order.status]}
          </span>
        </div>

        <div className="maintenance-order-meta">
          <span>Prioridad {PRIORITY_LABELS[order.priority].toLocaleLowerCase("es-ES")}</span>
          {order.estimated_minutes ? (
            <span>{order.estimated_minutes} min estimados</span>
          ) : null}
          <span>
            {order.assigned_to
              ? `Responsable: ${order.assigned_to.full_name}`
              : "Sin responsable"}
          </span>
          {overdue ? <strong>Vencida</strong> : null}
        </div>

        <div className="maintenance-order-actions">
          {canStart ? (
            <button
              className="accent-button"
              disabled={transitioningOrderId !== null}
              type="button"
              onClick={() =>
                void transitionOrder(
                  order,
                  "in_progress",
                  "Orden de mantenimiento iniciada.",
                )
              }
            >
              {isTransitioning ? "Actualizando…" : "Iniciar"}
            </button>
          ) : null}
          {canFinish ? (
            <button
              className="accent-button"
              disabled={transitioningOrderId !== null}
              type="button"
              onClick={() =>
                void transitionOrder(
                  order,
                  "completed",
                  "Orden de mantenimiento completada.",
                )
              }
            >
              {isTransitioning ? "Actualizando…" : "Completar"}
            </button>
          ) : null}
          {canManage && isOpen ? (
            <button
              className="secondary-button"
              disabled={transitioningOrderId !== null}
              type="button"
              onClick={(event) => {
                reasonTriggerButtonRef.current = event.currentTarget;
                setLoadError("");
                setReasonedTransition({
                  orderId: order.id,
                  status: "cancelled",
                  note: "",
                  successMessage: "Orden de mantenimiento cancelada.",
                });
              }}
            >
              Cancelar
            </button>
          ) : null}
          {canReopen ? (
            <button
              className="secondary-button"
              disabled={transitioningOrderId !== null}
              type="button"
              onClick={(event) => {
                reasonTriggerButtonRef.current = event.currentTarget;
                setLoadError("");
                setReasonedTransition({
                  orderId: order.id,
                  status: "planned",
                  note: "",
                  successMessage: "Orden de mantenimiento reabierta.",
                });
              }}
            >
              {isTransitioning ? "Actualizando…" : "Reabrir"}
            </button>
          ) : null}
          <button
            aria-expanded={detailOrderId === order.id}
            className="secondary-button"
            disabled={isLoadingDetail && detailOrderId === order.id}
            type="button"
            onClick={() => void loadDetail(order.id)}
          >
            {isLoadingDetail && detailOrderId === order.id
              ? "Cargando…"
              : detailOrderId === order.id && detail
                ? "Ocultar actividad"
                : "Ver actividad"}
          </button>
        </div>

        {reasonedTransition?.orderId === order.id ? (
          <form
            className="maintenance-transition-form"
            onSubmit={(event) => void handleReasonedTransition(event, order)}
          >
            <label>
              {reasonedTransition.status === "cancelled"
                ? "Motivo de cancelación"
                : "Motivo de reapertura"}
              <textarea
                disabled={isTransitioning}
                maxLength={2000}
                onChange={(event) =>
                  setReasonedTransition({
                    ...reasonedTransition,
                    note: event.target.value,
                  })
                }
                onKeyDown={(event) => {
                  if (event.key === "Escape" && !isTransitioning) {
                    closeReasonedTransition();
                  }
                }}
                placeholder="Explica brevemente el motivo para dejar trazabilidad"
                ref={transitionNoteRef}
                required
                rows={3}
                value={reasonedTransition.note}
              />
            </label>
            <div className="button-row">
              <button
                className="secondary-button"
                disabled={isTransitioning}
                onClick={closeReasonedTransition}
                type="button"
              >
                Volver
              </button>
              <button className="accent-button" disabled={isTransitioning} type="submit">
                {isTransitioning
                  ? "Guardando…"
                  : reasonedTransition.status === "cancelled"
                    ? "Confirmar cancelación"
                    : "Confirmar reapertura"}
              </button>
            </div>
          </form>
        ) : null}

        {detailOrderId === order.id ? (
          <div className="maintenance-event-panel" aria-live="polite">
            {detailError ? (
              <p className="maintenance-inline-error" role="alert">
                {detailError}
              </p>
            ) : null}
            {detail ? (
              detail.events.length > 0 ? (
                <ol className="maintenance-event-list">
                  {detail.events.map((event) => (
                    <li key={event.id}>
                      <strong>{eventLabel(event)}</strong>
                      <span>{formatDateTime(event.created_at)}</span>
                      {event.note ? <p>{event.note}</p> : null}
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="small-muted">Esta orden aún no tiene actividad.</p>
              )
            ) : null}
          </div>
        ) : null}
      </article>
    );
  }

  return (
    <section className="asset-maintenance-panel" aria-label="Mantenimiento del activo">
      <div className="asset-maintenance-heading">
        <div>
          <h3>Mantenimiento</h3>
          <span>
            {canView
              ? `${total} ${total === 1 ? "orden" : "órdenes"}`
              : "Consulta restringida"}
          </span>
        </div>
        {canCreate ? (
          <button
            className="accent-button"
            disabled={transitioningOrderId !== null}
            onClick={openDialog}
            ref={triggerButtonRef}
            type="button"
          >
            Programar
          </button>
        ) : null}
      </div>

      {statusMessage ? (
        <p className="maintenance-feedback" role="status">
          {statusMessage}
        </p>
      ) : null}
      {!canView ? (
        <p className="small-muted">
          No tienes permisos para consultar las órdenes de este activo.
          {canCreate ? " Sí puedes programar un nuevo mantenimiento." : ""}
        </p>
      ) : (
        <>
          {!canWriteContext ? (
            <p className="small-muted">
              El mantenimiento está en modo de solo lectura para este activo o
              esta organización.
            </p>
          ) : null}
          {loadError ? (
            <div className="maintenance-load-error" role="alert">
              <p>{loadError}</p>
              <button
                className="secondary-button"
                onClick={() => setReloadVersion((current) => current + 1)}
                type="button"
              >
                Actualizar órdenes
              </button>
            </div>
          ) : null}

          {isLoading ? (
            <p className="small-muted" role="status">
              Cargando mantenimiento…
            </p>
          ) : null}

          {!isLoading && !loadError && orders.length === 0 ? (
            <p className="maintenance-empty">
              No hay mantenimiento programado para este activo.
            </p>
          ) : null}

          {groupedOrders.overdue.length > 0 ? (
            <div className="maintenance-order-group">
              <h4>Vencidas</h4>
              {groupedOrders.overdue.map((order) => renderOrder(order, true))}
            </div>
          ) : null}

          {groupedOrders.upcoming.length > 0 ? (
            <div className="maintenance-order-group">
              <h4>Próximas</h4>
              {groupedOrders.upcoming.map((order) => renderOrder(order))}
            </div>
          ) : null}

          {groupedOrders.history.length > 0 ? (
            <div className="maintenance-order-group">
              <h4>Historial</h4>
              {groupedOrders.history.map((order) => renderOrder(order))}
            </div>
          ) : null}

          {total > orders.length ? (
            <p className="small-muted">
              Se muestran {orders.length} de {total} órdenes.
            </p>
          ) : null}
        </>
      )}

      {isDialogOpen ? (
        <div
          className="maintenance-dialog-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) {
              closeDialog();
            }
          }}
          role="presentation"
        >
          <form
            aria-describedby="maintenance-dialog-description"
            aria-labelledby="maintenance-dialog-title"
            aria-modal="true"
            className="maintenance-dialog"
            onSubmit={handleCreate}
            ref={dialogRef}
            role="dialog"
          >
            <div className="maintenance-dialog-heading">
              <div>
                <p className="eyebrow">{assetName}</p>
                <h2 id="maintenance-dialog-title">Programar mantenimiento</h2>
              </div>
              <button
                className="secondary-button"
                disabled={isCreating}
                onClick={closeDialog}
                type="button"
              >
                Cerrar
              </button>
            </div>
            <p className="small-muted" id="maintenance-dialog-description">
              Crea una orden asociada a este activo. La organización y el municipio
              se validan a partir del inventario.
            </p>

            <div className="maintenance-dialog-grid">
              <label className="maintenance-dialog-title-field">
                Trabajo previsto
                <input
                  autoComplete="off"
                  disabled={isCreating}
                  maxLength={255}
                  onChange={(event) =>
                    setDraft({ ...draft, title: event.target.value })
                  }
                  placeholder="Ej. Revisar sistema de climatización"
                  ref={titleInputRef}
                  required
                  value={draft.title}
                />
              </label>

              <label>
                Tipo
                <select
                  disabled={isCreating}
                  onChange={(event) =>
                    setDraft({
                      ...draft,
                      maintenanceType: event.target.value as MaintenanceType,
                    })
                  }
                  value={draft.maintenanceType}
                >
                  {Object.entries(TYPE_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>

              <label>
                Prioridad
                <select
                  disabled={isCreating}
                  onChange={(event) =>
                    setDraft({
                      ...draft,
                      priority: event.target.value as MaintenanceOrderPriority,
                    })
                  }
                  value={draft.priority}
                >
                  {Object.entries(PRIORITY_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>

              <label>
                Fecha prevista
                <input
                  disabled={isCreating}
                  min={localDateKey()}
                  onChange={(event) =>
                    setDraft({ ...draft, scheduledFor: event.target.value })
                  }
                  required
                  type="date"
                  value={draft.scheduledFor}
                />
              </label>

              <label>
                Duración estimada (minutos)
                <input
                  disabled={isCreating}
                  inputMode="numeric"
                  min="1"
                  onChange={(event) =>
                    setDraft({ ...draft, estimatedMinutes: event.target.value })
                  }
                  placeholder="Ej. 90"
                  step="1"
                  type="number"
                  value={draft.estimatedMinutes}
                />
              </label>

              <label className="maintenance-dialog-description-field">
                Indicaciones
                <textarea
                  disabled={isCreating}
                  maxLength={5000}
                  onChange={(event) =>
                    setDraft({ ...draft, description: event.target.value })
                  }
                  placeholder="Trabajo a realizar, materiales o comprobaciones…"
                  value={draft.description}
                />
              </label>
            </div>

            {formError ? (
              <p className="form-error" role="alert">
                {formError}
              </p>
            ) : null}

            <div className="button-row maintenance-dialog-actions">
              <button
                className="secondary-button"
                disabled={isCreating}
                onClick={closeDialog}
                type="button"
              >
                Cancelar
              </button>
              <button className="accent-button" disabled={isCreating} type="submit">
                {isCreating ? "Programando…" : "Programar mantenimiento"}
              </button>
            </div>
          </form>
        </div>
      ) : null}
    </section>
  );
}
