"use client";

import {
  Archive,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  RotateCcw,
  Save,
  XCircle,
} from "lucide-react";
import { useEffect, useState } from "react";
import type {
  ProductFeedbackStatusFilter,
  ProductFeedbackUpdate,
} from "../lib/admin/useProductFeedbackAdmin";
import {
  ASSISTANT_ADMIN_FEEDBACK_PRIORITIES,
  ASSISTANT_ADMIN_FEEDBACK_STATUSES,
  formatAssistantAdminFeedbackCategory,
  formatAssistantAdminFeedbackPriority,
  formatAssistantAdminFeedbackStatus,
  type AssistantAdminFeedback,
  type AssistantAdminFeedbackPriority,
  type AssistantAdminFeedbackStatus,
} from "./types";

type ProductFeedbackDraft = {
  feedbackId: number;
  priority: AssistantAdminFeedbackPriority;
  reviewNotes: string;
};

type ProductFeedbackAdminProps = {
  items: AssistantAdminFeedback[];
  total: number;
  page: number;
  pageSize: number;
  statusFilter: ProductFeedbackStatusFilter;
  selectedFeedbackId: number | null;
  isLoading: boolean;
  updatingFeedbackId: number | null;
  onStatusFilterChange: (status: ProductFeedbackStatusFilter) => void;
  onPrevPage: () => void;
  onNextPage: () => void;
  onSelectFeedback: (feedbackId: number) => void;
  onUpdateFeedback: (
    feedbackId: number,
    updates: ProductFeedbackUpdate,
  ) => Promise<boolean>;
};

function formatDateTime(value: string) {
  return new Date(value).toLocaleString("es-ES", {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function buildDraft(item: AssistantAdminFeedback): ProductFeedbackDraft {
  return {
    feedbackId: item.id,
    priority: item.priority,
    reviewNotes: item.review_notes ?? "",
  };
}

function organizationLabel(item: AssistantAdminFeedback) {
  if (item.organization?.name) {
    return item.organization.name;
  }
  return item.organization_id === null
    ? "Sin organización"
    : `Organización #${item.organization_id}`;
}

function sourceLabel(item: AssistantAdminFeedback) {
  const parts: string[] = [];
  if (item.source_conversation_id !== null) {
    parts.push(`conversación #${item.source_conversation_id}`);
  }
  if (item.source_message_id !== null) {
    parts.push(`mensaje #${item.source_message_id}`);
  }
  return parts.length > 0 ? parts.join(" · ") : "Sin referencia de origen";
}

export function ProductFeedbackAdmin({
  items,
  total,
  page,
  pageSize,
  statusFilter,
  selectedFeedbackId,
  isLoading,
  updatingFeedbackId,
  onStatusFilterChange,
  onPrevPage,
  onNextPage,
  onSelectFeedback,
  onUpdateFeedback,
}: ProductFeedbackAdminProps) {
  const [draft, setDraft] = useState<ProductFeedbackDraft | null>(null);

  const selectedItem =
    items.find((item) => item.id === selectedFeedbackId) ?? items[0] ?? null;

  useEffect(() => {
    if (!selectedItem) {
      setDraft(null);
      return;
    }
    setDraft(buildDraft(selectedItem));
  }, [selectedItem]);

  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const isUpdating = updatingFeedbackId !== null;
  const isBusy = isLoading || isUpdating;
  const draftMatchesSelection = draft?.feedbackId === selectedItem?.id;

  function selectItem(item: AssistantAdminFeedback) {
    onSelectFeedback(item.id);
    setDraft(buildDraft(item));
  }

  function updateDraft(updates: Partial<ProductFeedbackDraft>) {
    setDraft((current) => (current ? { ...current, ...updates } : current));
  }

  async function submitReview(status?: AssistantAdminFeedbackStatus) {
    if (!selectedItem || !draft || !draftMatchesSelection) {
      return;
    }
    await onUpdateFeedback(selectedItem.id, {
      priority: draft.priority,
      review_notes: draft.reviewNotes.trim() || null,
      ...(status ? { status } : {}),
    });
  }

  return (
    <div className="review-inbox">
      <div className="review-toolbar">
        <label>
          Estado
          <select
            value={statusFilter}
            onChange={(event) =>
              onStatusFilterChange(
                event.target.value as ProductFeedbackStatusFilter,
              )
            }
            disabled={isBusy}
          >
            <option value="all">Todos</option>
            {ASSISTANT_ADMIN_FEEDBACK_STATUSES.map((status) => (
              <option key={status} value={status}>
                {formatAssistantAdminFeedbackStatus(status)}
              </option>
            ))}
          </select>
        </label>
        <span className="review-result-count">
          {total} {total === 1 ? "registro" : "registros"}
        </span>
      </div>

      <div className="review-inbox-layout">
        <div className="review-list" aria-label="Feedback de producto">
          {items.length > 0 ? (
            items.map((item) => (
              <button
                aria-pressed={selectedItem?.id === item.id}
                className={
                  selectedItem?.id === item.id
                    ? "review-list-item selected"
                    : "review-list-item"
                }
                key={item.id}
                disabled={isBusy}
                onClick={() => selectItem(item)}
                type="button"
              >
                <span className="review-list-heading">
                  <strong>{item.title}</strong>
                  <span className={`tag priority-${item.priority}`}>
                    {formatAssistantAdminFeedbackPriority(item.priority)}
                  </span>
                </span>
                <span className="review-list-summary">{item.description}</span>
                <span>{organizationLabel(item)}</span>
                <span>{formatDateTime(item.updated_at)}</span>
              </button>
            ))
          ) : (
            <p className="review-empty-state">
              {isLoading
                ? "Cargando feedback de producto..."
                : "No hay feedback con este estado."}
            </p>
          )}

          <div className="review-pager">
            <button
              aria-label="Página anterior"
              className="secondary-button icon-button"
              disabled={page <= 1 || isBusy}
              onClick={onPrevPage}
              title="Página anterior"
              type="button"
            >
              <ChevronLeft aria-hidden size={17} />
            </button>
            <span className="pager-status">
              Página {page} de {pageCount}
            </span>
            <button
              aria-label="Página siguiente"
              className="secondary-button icon-button"
              disabled={page >= pageCount || isBusy}
              onClick={onNextPage}
              title="Página siguiente"
              type="button"
            >
              <ChevronRight aria-hidden size={17} />
            </button>
          </div>
        </div>

        <div className="review-detail">
          {selectedItem && draftMatchesSelection && draft ? (
            <>
              <div className="review-detail-header">
                <div>
                  <p className="small-muted">Feedback #{selectedItem.id}</p>
                  <h3>{selectedItem.title}</h3>
                </div>
                <span className="tag">
                  {formatAssistantAdminFeedbackStatus(selectedItem.status)}
                </span>
              </div>

              <p className="review-description">{selectedItem.description}</p>

              <dl className="review-metadata">
                <div>
                  <dt>Categoría</dt>
                  <dd>
                    {formatAssistantAdminFeedbackCategory(selectedItem.category)}
                  </dd>
                </div>
                <div>
                  <dt>Organización</dt>
                  <dd>{organizationLabel(selectedItem)}</dd>
                </div>
                <div>
                  <dt>Enviado por</dt>
                  <dd>
                    {selectedItem.submitted_by
                      ? `${selectedItem.submitted_by.full_name} (${selectedItem.submitted_by.email})`
                      : "Usuario no disponible"}
                  </dd>
                </div>
                <div>
                  <dt>Registrado</dt>
                  <dd>{formatDateTime(selectedItem.created_at)}</dd>
                </div>
                <div>
                  <dt>Origen</dt>
                  <dd>{sourceLabel(selectedItem)}</dd>
                </div>
                {selectedItem.reviewed_by ? (
                  <div>
                    <dt>Última revisión</dt>
                    <dd>
                      {selectedItem.reviewed_by.full_name}
                      {selectedItem.reviewed_at
                        ? ` · ${formatDateTime(selectedItem.reviewed_at)}`
                        : ""}
                    </dd>
                  </div>
                ) : null}
              </dl>

              <div className="review-edit-form">
                <label>
                  Prioridad
                  <select
                    disabled={isBusy}
                    value={draft.priority}
                    onChange={(event) =>
                      updateDraft({
                        priority: event.target
                          .value as AssistantAdminFeedbackPriority,
                      })
                    }
                  >
                    {ASSISTANT_ADMIN_FEEDBACK_PRIORITIES.map((priority) => (
                      <option key={priority} value={priority}>
                        {formatAssistantAdminFeedbackPriority(priority)}
                      </option>
                    ))}
                  </select>
                </label>

                <label>
                  Notas de revisión
                  <textarea
                    disabled={isBusy}
                    maxLength={2000}
                    onChange={(event) =>
                      updateDraft({ reviewNotes: event.target.value })
                    }
                    rows={5}
                    value={draft.reviewNotes}
                  />
                </label>

                <div className="review-actions">
                  <button
                    className="secondary-button"
                    disabled={isBusy}
                    onClick={() => void submitReview()}
                    type="button"
                  >
                    <Save aria-hidden size={16} />
                    Guardar cambios
                  </button>
                  {selectedItem.status === "submitted" ||
                  selectedItem.status === "dismissed" ? (
                    <button
                      disabled={isBusy}
                      onClick={() => void submitReview("reviewed")}
                      type="button"
                    >
                      <CheckCircle2 aria-hidden size={16} />
                      Marcar revisado
                    </button>
                  ) : null}
                  {selectedItem.status === "submitted" ||
                  selectedItem.status === "reviewed" ? (
                    <button
                      className="secondary-button"
                      disabled={isBusy}
                      onClick={() => void submitReview("dismissed")}
                      type="button"
                    >
                      <XCircle aria-hidden size={16} />
                      Descartar
                    </button>
                  ) : null}
                  {selectedItem.status !== "archived" ? (
                    <button
                      className="secondary-button"
                      disabled={isBusy}
                      onClick={() => void submitReview("archived")}
                      type="button"
                    >
                      <Archive aria-hidden size={16} />
                      Archivar
                    </button>
                  ) : null}
                  {selectedItem.status !== "submitted" ? (
                    <button
                      className="secondary-button"
                      disabled={isBusy}
                      onClick={() => void submitReview("submitted")}
                      type="button"
                    >
                      <RotateCcw aria-hidden size={16} />
                      Reabrir
                    </button>
                  ) : null}
                </div>
              </div>
            </>
          ) : (
            <p className="review-empty-state">
              Selecciona un registro para revisar su detalle.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
