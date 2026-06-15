import { type FormEvent, type MouseEvent, useEffect, useRef, useState } from "react";
import {
  ASSISTANT_MEMORY_CATEGORY_LABELS,
  ASSISTANT_MEMORY_SENSITIVITY_LABELS,
  formatAssistantTool,
  type AssistantConversation,
  type AssistantConversationDetail,
  type AssistantMemoryCategory,
  type AssistantMemoryEntry,
  type AssistantMemorySensitivity,
  type AssistantMemoryStatus,
  type AssistantStatus,
} from "./types";

// Minimal local typings for the Web Speech API; the DOM lib does not ship
// them and we do not want an extra dependency just for dictation.
type SpeechRecognitionAlternativeLike = {
  transcript: string;
};

type SpeechRecognitionResultLike = {
  isFinal: boolean;
  0: SpeechRecognitionAlternativeLike;
};

type SpeechRecognitionEventLike = {
  resultIndex: number;
  results: {
    length: number;
    [index: number]: SpeechRecognitionResultLike;
  };
};

type SpeechRecognitionErrorEventLike = {
  error: string;
};

type SpeechRecognitionLike = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  processLocally?: boolean;
  onresult: ((event: SpeechRecognitionEventLike) => void) | null;
  onerror: ((event: SpeechRecognitionErrorEventLike) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
};

type SpeechRecognitionConstructor = (new () => SpeechRecognitionLike) & {
  available?: (options: {
    langs: string[];
    processLocally: boolean;
  }) => Promise<string>;
};

function getSpeechRecognitionConstructor(): SpeechRecognitionConstructor | null {
  if (typeof window === "undefined") {
    return null;
  }

  return (
    ((window as any).SpeechRecognition as
      | SpeechRecognitionConstructor
      | undefined) ??
    ((window as any).webkitSpeechRecognition as
      | SpeechRecognitionConstructor
      | undefined) ??
    null
  );
}

type AssistantPanelProps = {
  assistantStatus: AssistantStatus | null;
  conversations: AssistantConversation[];
  memoryEntries: AssistantMemoryEntry[];
  selectedConversation: AssistantConversationDetail | null;
  draftMessage: string;
  isLoadingAssistant: boolean;
  isSendingMessage: boolean;
  assistantError: string;
  includeArchivedConversations: boolean;
  onDraftMessageChange: (value: string) => void;
  onSelectConversation: (conversationId: number) => void;
  onStartConversation: () => void;
  onSendMessage: () => void;
  onArchiveConversation: (conversationId: number) => void;
  onRestoreConversation: (conversationId: number) => void;
  onIncludeArchivedConversationsChange: (includeArchived: boolean) => void;
  onUpdateMemoryEntry: (
    entryId: number,
    updates: {
      category?: AssistantMemoryCategory;
      content?: string;
      status?: AssistantMemoryStatus;
      sensitivity?: AssistantMemorySensitivity;
      review_notes?: string;
    },
  ) => void;
};

export function AssistantPanel({
  assistantStatus,
  conversations,
  memoryEntries,
  selectedConversation,
  draftMessage,
  isLoadingAssistant,
  isSendingMessage,
  assistantError,
  includeArchivedConversations,
  onDraftMessageChange,
  onSelectConversation,
  onStartConversation,
  onSendMessage,
  onArchiveConversation,
  onRestoreConversation,
  onIncludeArchivedConversationsChange,
  onUpdateMemoryEntry,
}: AssistantPanelProps) {
  const assistantDisabled = assistantStatus !== null && !assistantStatus.enabled;
  const selectedIsArchived = selectedConversation?.status === "archived";

  const [isListening, setIsListening] = useState(false);
  const [voiceError, setVoiceError] = useState("");
  const [speechSupported, setSpeechSupported] = useState(false);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  // Mirrors the draft so recognition callbacks append to the latest value
  // instead of the one captured when listening started.
  const draftMessageRef = useRef(draftMessage);

  useEffect(() => {
    draftMessageRef.current = draftMessage;
  }, [draftMessage]);

  useEffect(() => {
    // Detected in an effect to avoid a hydration mismatch on the button.
    // Privacy rule: only ON-DEVICE recognition is acceptable — the browser's
    // default cloud mode sends municipal audio to the vendor's servers
    // without a DPA, so without a local-availability guarantee the feature
    // stays off (see docs/investigacion-api-ia.md §6).
    const SpeechRecognitionImpl = getSpeechRecognitionConstructor();
    if (!SpeechRecognitionImpl || typeof SpeechRecognitionImpl.available !== "function") {
      setSpeechSupported(false);
      return;
    }

    let isActive = true;
    SpeechRecognitionImpl.available({ langs: ["es-ES"], processLocally: true })
      .then((availability) => {
        if (isActive) {
          setSpeechSupported(availability !== "unavailable");
        }
      })
      .catch(() => {
        if (isActive) {
          setSpeechSupported(false);
        }
      });

    return () => {
      isActive = false;
    };
  }, []);

  function detachRecognition() {
    const recognition = recognitionRef.current;
    recognitionRef.current = null;
    if (recognition) {
      recognition.onresult = null;
      recognition.onerror = null;
      recognition.onend = null;
      recognition.stop();
    }
  }

  function stopListening() {
    detachRecognition();
    setIsListening(false);
  }

  useEffect(() => {
    return () => {
      detachRecognition();
    };
  }, []);

  function startListening() {
    const SpeechRecognitionImpl = getSpeechRecognitionConstructor();
    if (!SpeechRecognitionImpl || recognitionRef.current) {
      return;
    }

    setVoiceError("");

    const recognition = new SpeechRecognitionImpl();
    recognition.lang = "es-ES";
    recognition.continuous = true;
    recognition.interimResults = true;
    // Never fall back to the browser's cloud recognition service.
    recognition.processLocally = true;
    recognition.onresult = (event) => {
      let transcript = "";
      for (
        let index = event.resultIndex;
        index < event.results.length;
        index += 1
      ) {
        const result = event.results[index];
        if (result.isFinal) {
          transcript += result[0].transcript;
        }
      }

      const chunk = transcript.trim();
      if (!chunk) {
        return;
      }

      const currentDraft = draftMessageRef.current;
      onDraftMessageChange(currentDraft ? `${currentDraft} ${chunk}` : chunk);
    };
    recognition.onerror = (event) => {
      stopListening();
      if (event.error !== "aborted") {
        setVoiceError(
          "No se pudo usar el dictado por voz. Revisa los permisos del micrófono.",
        );
      }
    };
    recognition.onend = () => {
      // The browser can end recognition on its own (silence, network…).
      if (recognitionRef.current === recognition) {
        recognitionRef.current = null;
        setIsListening(false);
      }
    };

    recognitionRef.current = recognition;
    recognition.start();
    setIsListening(true);
  }

  function handleToggleListening() {
    if (isListening) {
      stopListening();
    } else {
      startListening();
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    stopListening();
    onSendMessage();
  }

  function handleMemoryEdit(
    event: FormEvent<HTMLFormElement>,
    entryId: number,
  ) {
    event.preventDefault();
    const formData = new FormData(event.currentTarget);
    onUpdateMemoryEntry(entryId, {
      category: formData.get("category") as AssistantMemoryCategory,
      content: String(formData.get("content") ?? ""),
      sensitivity: formData.get("sensitivity") as AssistantMemorySensitivity,
      review_notes: String(formData.get("review_notes") ?? ""),
    });
  }

  function handleMemoryStatus(
    event: MouseEvent<HTMLButtonElement>,
    entryId: number,
    status: AssistantMemoryStatus,
  ) {
    const form = event.currentTarget.form;
    if (!form) {
      onUpdateMemoryEntry(entryId, { status });
      return;
    }

    const formData = new FormData(form);
    onUpdateMemoryEntry(entryId, {
      category: formData.get("category") as AssistantMemoryCategory,
      content: String(formData.get("content") ?? ""),
      sensitivity: formData.get("sensitivity") as AssistantMemorySensitivity,
      review_notes: String(formData.get("review_notes") ?? ""),
      status,
    });
  }

  return (
    <section className="panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">Asistente</p>
          <h2>Captura de requisitos por conversación</h2>
          <p className="muted">
            Cuéntale al asistente qué necesita tu ayuntamiento y él lo
            registrará como requisitos en borrador para su revisión.
          </p>
        </div>
        <button
          className="secondary-button"
          type="button"
          onClick={onStartConversation}
          disabled={isLoadingAssistant || isSendingMessage || assistantDisabled}
        >
          Nueva conversación
        </button>
      </div>

      {assistantError ? <p className="error-message">{assistantError}</p> : null}

      {assistantDisabled ? (
        <p className="muted">
          El asistente no está configurado en este servidor (falta la clave de
          la API de IA). Contacta con el administrador.
        </p>
      ) : null}

      {memoryEntries.length > 0 ? (
        <section className="assistant-memory-review">
          <div className="assistant-memory-review-header">
            <div>
              <p className="eyebrow">Memoria pendiente</p>
              <h3>Conocimiento propuesto por el asistente</h3>
            </div>
            <span className="tag">{memoryEntries.length}</span>
          </div>
          <div className="assistant-memory-list">
            {memoryEntries.map((entry) => (
              <form
                className="assistant-memory-item"
                key={entry.id}
                onSubmit={(event) => handleMemoryEdit(event, entry.id)}
              >
                <textarea
                  name="content"
                  defaultValue={entry.content}
                  rows={3}
                  disabled={isSendingMessage}
                />
                <div className="assistant-memory-fields">
                  <label>
                    Tipo
                    <select
                      name="category"
                      defaultValue={entry.category}
                      disabled={isSendingMessage}
                    >
                      {Object.entries(ASSISTANT_MEMORY_CATEGORY_LABELS).map(
                        ([value, label]) => (
                          <option key={value} value={value}>
                            {label}
                          </option>
                        ),
                      )}
                    </select>
                  </label>
                  <label>
                    Sensibilidad
                    <select
                      name="sensitivity"
                      defaultValue={entry.sensitivity}
                      disabled={isSendingMessage}
                    >
                      {Object.entries(ASSISTANT_MEMORY_SENSITIVITY_LABELS).map(
                        ([value, label]) => (
                          <option key={value} value={value}>
                            {label}
                          </option>
                        ),
                      )}
                    </select>
                  </label>
                </div>
                <input
                  name="review_notes"
                  placeholder="Nota de revisión"
                  disabled={isSendingMessage}
                />
                <div className="assistant-memory-actions">
                  <button type="submit" disabled={isSendingMessage}>
                    Guardar
                  </button>
                  <button
                    type="button"
                    disabled={isSendingMessage}
                    onClick={(event) =>
                      handleMemoryStatus(event, entry.id, "approved")
                    }
                  >
                    Aprobar
                  </button>
                  <button
                    className="secondary-button"
                    type="button"
                    disabled={isSendingMessage}
                    onClick={(event) =>
                      handleMemoryStatus(event, entry.id, "rejected")
                    }
                  >
                    Rechazar
                  </button>
                  <button
                    className="danger-button"
                    type="button"
                    disabled={isSendingMessage}
                    onClick={(event) =>
                      handleMemoryStatus(event, entry.id, "blocked")
                    }
                  >
                    Bloquear
                  </button>
                </div>
              </form>
            ))}
          </div>
        </section>
      ) : null}

      <div className="assistant-layout">
        <aside className="assistant-conversations">
          <label className="checkbox-label assistant-archived-toggle">
            <input
              checked={includeArchivedConversations}
              onChange={(event) =>
                onIncludeArchivedConversationsChange(event.target.checked)
              }
              type="checkbox"
              disabled={isLoadingAssistant || isSendingMessage}
            />
            Mostrar archivadas
          </label>
          {isLoadingAssistant ? (
            <p className="muted">Cargando conversaciones…</p>
          ) : null}
          {!isLoadingAssistant && conversations.length === 0 ? (
            <p className="muted">Todavía no hay conversaciones.</p>
          ) : null}
          <ul>
            {conversations.map((conversation) => (
              <li key={conversation.id}>
                <button
                  type="button"
                  className={
                    selectedConversation?.id === conversation.id
                      ? "assistant-conversation-item selected"
                      : "assistant-conversation-item"
                  }
                  onClick={() => onSelectConversation(conversation.id)}
                  disabled={isSendingMessage}
                >
                  <span className="assistant-conversation-title">
                    {conversation.title}
                  </span>
                  {conversation.status === "archived" ? (
                    <span className="tag assistant-archived-tag">
                      Archivada
                    </span>
                  ) : null}
                  <span className="muted">
                    {new Date(conversation.updated_at).toLocaleDateString("es-ES")}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </aside>

        <div className="assistant-thread">
          {selectedConversation ? (
            <>
              <div className="assistant-thread-header">
                <h3>{selectedConversation.title}</h3>
                {selectedIsArchived ? (
                  <button
                    className="secondary-button"
                    type="button"
                    disabled={isSendingMessage}
                    onClick={() =>
                      onRestoreConversation(selectedConversation.id)
                    }
                  >
                    Restaurar
                  </button>
                ) : (
                  <button
                    className="danger-button"
                    type="button"
                    disabled={isSendingMessage}
                    onClick={() => {
                      if (
                        window.confirm(
                          "¿Archivar esta conversación? Dejará de aparecer en la lista.",
                        )
                      ) {
                        onArchiveConversation(selectedConversation.id);
                      }
                    }}
                  >
                    Archivar
                  </button>
                )}
              </div>

              <div className="assistant-messages">
                {selectedConversation.messages.length === 0 ? (
                  <p className="muted">
                    Escribe tu primer mensaje. Por ejemplo: «Necesitamos que los
                    vecinos puedan pedir cita previa para el padrón».
                  </p>
                ) : null}
                {selectedConversation.messages.map((message) => (
                  <div
                    key={message.id}
                    className={`assistant-message ${message.role}`}
                  >
                    <p className="assistant-message-content">{message.content}</p>
                    {message.actions.length > 0 ? (
                      <div className="assistant-actions">
                        {message.actions.map((action, index) => (
                          <span
                            key={`${message.id}-${index}`}
                            className={
                              action.ok
                                ? "tag assistant-action-ok"
                                : "tag assistant-action-error"
                            }
                            title={action.result}
                          >
                            {action.ok ? "✓" : "✗"} {formatAssistantTool(action.tool)}
                          </span>
                        ))}
                      </div>
                    ) : null}
                  </div>
                ))}
                {isSendingMessage ? (
                  <div className="assistant-message assistant">
                    <p className="assistant-message-content muted">
                      El asistente está trabajando…
                    </p>
                  </div>
                ) : null}
              </div>

              {selectedIsArchived ? (
                <p className="muted">
                  Esta conversación está archivada. Restáurala para seguir
                  escribiendo.
                </p>
              ) : null}

              <form className="assistant-composer" onSubmit={handleSubmit}>
                <textarea
                  value={draftMessage}
                  onChange={(event) => onDraftMessageChange(event.target.value)}
                  placeholder="Escribe tu mensaje…"
                  rows={3}
                  disabled={
                    isSendingMessage || assistantDisabled || selectedIsArchived
                  }
                />
                <button
                  type="button"
                  className={
                    isListening ? "assistant-mic recording" : "assistant-mic"
                  }
                  onClick={handleToggleListening}
                  disabled={
                    !speechSupported ||
                    isSendingMessage ||
                    assistantDisabled ||
                    selectedIsArchived
                  }
                  title={
                    speechSupported
                      ? "El audio se procesa localmente en tu equipo; no se envía a servidores externos"
                      : "El dictado local no está disponible en este navegador (requiere Chrome reciente con reconocimiento en el dispositivo)"
                  }
                >
                  {isListening ? "Detener" : "Dictar"}
                </button>
                <button
                  type="submit"
                  disabled={
                    isSendingMessage ||
                    assistantDisabled ||
                    selectedIsArchived ||
                    draftMessage.trim().length === 0
                  }
                >
                  {isSendingMessage ? "Enviando…" : "Enviar"}
                </button>
              </form>

              {voiceError ? (
                <p className="error-message assistant-voice-error">
                  {voiceError}
                </p>
              ) : null}
            </>
          ) : (
            <p className="muted">
              Selecciona una conversación o crea una nueva para empezar.
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
