"use client";

import { useEffect, useRef } from "react";

type ConfirmDialogProps = {
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
};

/** Confirmación modal para acciones destructivas, en lugar de window.confirm. */
export function ConfirmDialog({
  message,
  confirmLabel = "Sí, eliminar",
  cancelLabel = "No",
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);

  // El foco entra por la opción segura: confirmar debe ser deliberado.
  useEffect(() => {
    cancelRef.current?.focus();
  }, []);

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        onCancel();
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [onCancel]);

  return (
    <div className="confirm-scrim" onClick={onCancel} role="presentation">
      <div
        aria-modal="true"
        className="confirm-dialog"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
      >
        <p className="confirm-message">{message}</p>
        <div className="confirm-actions">
          <button
            className="secondary-button"
            onClick={onCancel}
            ref={cancelRef}
            type="button"
          >
            {cancelLabel}
          </button>
          <button className="confirm-danger" onClick={onConfirm} type="button">
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
