"use client";

import {
  Archive,
  Ban,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  RotateCcw,
  Save,
  XCircle,
} from "lucide-react";
import { useEffect, useState } from "react";
import type { MemoryReviewUpdate } from "../lib/admin/useMemoryReviewAdmin";
import {
  ASSISTANT_MEMORY_CATEGORIES,
  ASSISTANT_MEMORY_CATEGORY_LABELS,
  ASSISTANT_MEMORY_SENSITIVITIES,
  ASSISTANT_MEMORY_SENSITIVITY_LABELS,
  ASSISTANT_MEMORY_STATUSES,
  formatAssistantMemoryStatus,
  type AssistantMemoryCategory,
  type AssistantMemoryEntry,
  type AssistantMemorySensitivity,
  type AssistantMemoryStatus,
} from "./types";

type MemoryReviewDraft = {
  entryId: number;
  category: AssistantMemoryCategory;
  content: string;
  sensitivity: AssistantMemorySensitivity;
  reviewNotes: string;
};

type MemoryReviewAdminProps = {
  entries: AssistantMemoryEntry[];
  total: number;
  page: number;
  pageSize: number;
  statusFilter: AssistantMemoryStatus;
  selectedEntryId: number | null;
  isLoading: boolean;
  updatingEntryId: number | null;
  onStatusFilterChange: (status: AssistantMemoryStatus) => void;
  onPrevPage: () => void;
  onNextPage: () => void;
  onSelectEntry: (entryId: number) => void;
  onUpdateEntry: (
    entryId: number,
    updates: MemoryReviewUpdate,
  ) => Promise<boolean>;
};

function formatDateTime(value: string) {
  return new Date(value).toLocaleString("es-ES", {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function buildDraft(entry: AssistantMemoryEntry): MemoryReviewDraft {
  return {
    entryId: entry.id,
    category: entry.category,
    content: entry.content,
    sensitivity: entry.sensitivity,
    reviewNotes: entry.review_notes ?? "",
  };
}

function organizationLabel(entry: AssistantMemoryEntry) {
  return entry.organization?.name ?? `Organización #${entry.organization_id}`;
}

function sourceLabel(entry: AssistantMemoryEntry) {
  const parts: string[] = [];
  if (entry.source_conversation_id !== null) {
    parts.push(`conversación #${entry.source_conversation_id}`);
  }
  if (entry.source_message_id !== null) {
    parts.push(`mensaje #${entry.source_message_id}`);
  }
  return parts.length > 0 ? parts.join(" · ") : "Sin referencia de origen";
}

export function MemoryReviewAdmin({
  entries,
  total,
  page,
  pageSize,
  statusFilter,
  selectedEntryId,
  isLoading,
  updatingEntryId,
  onStatusFilterChange,
  onPrevPage,
  onNextPage,
  onSelectEntry,
  onUpdateEntry,
}: MemoryReviewAdminProps) {
  const [draft, setDraft] = useState<MemoryReviewDraft | null>(null);
  const [localError, setLocalError] = useState("");
  const [sensitiveApprovalConfirmed, setSensitiveApprovalConfirmed] =
    useState(false);

  const selectedEntry =
    entries.find((entry) => entry.id === selectedEntryId) ?? entries[0] ?? null;

  useEffect(() => {
    if (!selectedEntry) {
      setDraft(null);
      return;
    }
    setDraft(buildDraft(selectedEntry));
    setSensitiveApprovalConfirmed(false);
    setLocalError("");
  }, [selectedEntry]);

  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const isUpdating = updatingEntryId !== null;
  const isBusy = isLoading || isUpdating;
  const draftMatchesSelection = draft?.entryId === selectedEntry?.id;
  const hasSensitiveContent = Boolean(
    selectedEntry &&
      draft &&
      (selectedEntry.sensitivity !== "normal" || draft.sensitivity !== "normal"),
  );

  function selectEntry(entry: AssistantMemoryEntry) {
    onSelectEntry(entry.id);
    setDraft(buildDraft(entry));
    setSensitiveApprovalConfirmed(false);
    setLocalError("");
  }

  function updateDraft(updates: Partial<MemoryReviewDraft>) {
    setDraft((current) => (current ? { ...current, ...updates } : current));
  }

  async function submitReview(status?: AssistantMemoryStatus) {
    if (!selectedEntry || !draft || !draftMatchesSelection) {
      return;
    }

    const content = draft.content.trim();
    if (!content) {
      setLocalError("El contenido de memoria no puede quedar vacío.");
      return;
    }

    const confirmsSensitiveApproval =
      hasSensitiveContent && status === "approved";
    if (confirmsSensitiveApproval && !sensitiveApprovalConfirmed) {
      setLocalError(
        "Confirma expresamente la revisión del contenido sensible antes de aprobar esta memoria.",
      );
      return;
    }

    setLocalError("");
    const reviewNotes = draft.reviewNotes.trim() || null;
    const updates: MemoryReviewUpdate = {
      category: draft.category,
      content,
      sensitivity: draft.sensitivity,
      review_notes: reviewNotes,
      ...(confirmsSensitiveApproval
        ? { sensitive_approval_confirmed: true }
        : {}),
      ...(status ? { status } : {}),
    };
    const saved = await onUpdateEntry(selectedEntry.id, updates);
    if (saved) {
      setSensitiveApprovalConfirmed(false);
    }
  }

  return (
    <div className="review-inbox">
      <div className="review-toolbar">
        <label>
          Estado
          <select
            value={statusFilter}
            onChange={(event) =>
              onStatusFilterChange(event.target.value as AssistantMemoryStatus)
            }
            disabled={isBusy}
          >
            {ASSISTANT_MEMORY_STATUSES.map((status) => (
              <option key={status} value={status}>
                {formatAssistantMemoryStatus(status)}
              </option>
            ))}
          </select>
        </label>
        <span className="review-result-count">
          {total} {total === 1 ? "entrada" : "entradas"}
        </span>
      </div>

      <div className="review-inbox-layout">
        <div className="review-list" aria-label="Entradas de memoria">
          {entries.length > 0 ? (
            entries.map((entry) => (
              <button
                aria-pressed={selectedEntry?.id === entry.id}
                className={
                  selectedEntry?.id === entry.id
                    ? "review-list-item selected"
                    : "review-list-item"
                }
                key={entry.id}
                disabled={isBusy}
                onClick={() => selectEntry(entry)}
                type="button"
              >
                <span className="review-list-heading">
                  <strong>{ASSISTANT_MEMORY_CATEGORY_LABELS[entry.category]}</strong>
                  <span className="tag">
                    {ASSISTANT_MEMORY_SENSITIVITY_LABELS[entry.sensitivity]}
                  </span>
                </span>
                <span className="review-list-summary">{entry.content}</span>
                <span>{organizationLabel(entry)}</span>
                <span>{formatDateTime(entry.updated_at)}</span>
              </button>
            ))
          ) : (
            <p className="review-empty-state">
              {isLoading
                ? "Cargando entradas de memoria..."
                : "No hay entradas con este estado."}
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
          {selectedEntry && draftMatchesSelection && draft ? (
            <>
              <div className="review-detail-header">
                <div>
                  <p className="small-muted">Memoria #{selectedEntry.id}</p>
                  <h3>{ASSISTANT_MEMORY_CATEGORY_LABELS[selectedEntry.category]}</h3>
                </div>
                <span className="tag">
                  {formatAssistantMemoryStatus(selectedEntry.status)}
                </span>
              </div>

              <dl className="review-metadata">
                <div>
                  <dt>Organización</dt>
                  <dd>{organizationLabel(selectedEntry)}</dd>
                </div>
                <div>
                  <dt>Propuesta por</dt>
                  <dd>
                    {selectedEntry.proposed_by
                      ? `${selectedEntry.proposed_by.full_name} (${selectedEntry.proposed_by.email})`
                      : "Usuario no disponible"}
                  </dd>
                </div>
                <div>
                  <dt>Actualizada</dt>
                  <dd>{formatDateTime(selectedEntry.updated_at)}</dd>
                </div>
                <div>
                  <dt>Origen</dt>
                  <dd>{sourceLabel(selectedEntry)}</dd>
                </div>
                {selectedEntry.reviewed_by ? (
                  <div>
                    <dt>Última revisión</dt>
                    <dd>
                      {selectedEntry.reviewed_by.full_name}
                      {selectedEntry.reviewed_at
                        ? ` · ${formatDateTime(selectedEntry.reviewed_at)}`
                        : ""}
                    </dd>
                  </div>
                ) : null}
              </dl>

              <div className="review-edit-form">
                <div className="review-field-grid">
                  <label>
                    Categoría
                    <select
                      disabled={isBusy}
                      value={draft.category}
                      onChange={(event) => {
                        updateDraft({
                          category: event.target
                            .value as AssistantMemoryCategory,
                        });
                        setSensitiveApprovalConfirmed(false);
                      }}
                    >
                      {ASSISTANT_MEMORY_CATEGORIES.map((category) => (
                        <option key={category} value={category}>
                          {ASSISTANT_MEMORY_CATEGORY_LABELS[category]}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Sensibilidad
                    <select
                      disabled={isBusy}
                      value={draft.sensitivity}
                      onChange={(event) => {
                        updateDraft({
                          sensitivity: event.target
                            .value as AssistantMemorySensitivity,
                        });
                        setSensitiveApprovalConfirmed(false);
                      }}
                    >
                      {ASSISTANT_MEMORY_SENSITIVITIES.map((sensitivity) => (
                        <option key={sensitivity} value={sensitivity}>
                          {ASSISTANT_MEMORY_SENSITIVITY_LABELS[sensitivity]}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>

                <label>
                  Contenido institucional
                  <textarea
                    disabled={isBusy}
                    maxLength={1000}
                    onChange={(event) => {
                      updateDraft({ content: event.target.value });
                      setSensitiveApprovalConfirmed(false);
                    }}
                    rows={6}
                    value={draft.content}
                  />
                </label>

                <label>
                  Notas de revisión
                  <textarea
                    disabled={isBusy}
                    maxLength={2000}
                    onChange={(event) =>
                      updateDraft({ reviewNotes: event.target.value })
                    }
                    rows={4}
                    value={draft.reviewNotes}
                  />
                </label>

                {hasSensitiveContent ? (
                  <label className="checkbox-label review-sensitive-confirmation">
                    <input
                      checked={sensitiveApprovalConfirmed}
                      disabled={isBusy}
                      onChange={(event) =>
                        setSensitiveApprovalConfirmed(event.target.checked)
                      }
                      type="checkbox"
                    />
                    Confirmo que he revisado expresamente el contenido sensible
                    antes de aprobarlo.
                  </label>
                ) : null}

                {localError ? (
                  <p className="error-message">{localError}</p>
                ) : null}

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
                  {selectedEntry.status === "proposed" ||
                  selectedEntry.status === "approved" ? (
                    <button
                      disabled={isBusy}
                      onClick={() => void submitReview("approved")}
                      type="button"
                    >
                      <CheckCircle2 aria-hidden size={16} />
                      {selectedEntry.status === "approved"
                        ? "Reaprobar cambios"
                        : "Aprobar"}
                    </button>
                  ) : null}
                  {selectedEntry.status === "proposed" ||
                  selectedEntry.status === "approved" ||
                  selectedEntry.status === "blocked" ? (
                    <button
                      className="secondary-button"
                      disabled={isBusy}
                      onClick={() => void submitReview("rejected")}
                      type="button"
                    >
                      <XCircle aria-hidden size={16} />
                      Rechazar
                    </button>
                  ) : null}
                  {selectedEntry.status === "proposed" ||
                  selectedEntry.status === "approved" ? (
                    <button
                      className="secondary-button"
                      disabled={isBusy}
                      onClick={() => void submitReview("blocked")}
                      type="button"
                    >
                      <Ban aria-hidden size={16} />
                      Bloquear
                    </button>
                  ) : null}
                  {selectedEntry.status !== "archived" ? (
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
                  {selectedEntry.status !== "proposed" ? (
                    <button
                      className="secondary-button"
                      disabled={isBusy}
                      onClick={() => void submitReview("proposed")}
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
              Selecciona una entrada para revisar su contenido.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
