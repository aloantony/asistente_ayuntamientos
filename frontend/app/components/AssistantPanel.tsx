import { type FormEvent } from "react";
import {
  formatAssistantTool,
  type AssistantConversation,
  type AssistantConversationDetail,
  type AssistantStatus,
} from "./types";

type AssistantPanelProps = {
  assistantStatus: AssistantStatus | null;
  conversations: AssistantConversation[];
  selectedConversation: AssistantConversationDetail | null;
  draftMessage: string;
  isLoadingAssistant: boolean;
  isSendingMessage: boolean;
  assistantError: string;
  onDraftMessageChange: (value: string) => void;
  onSelectConversation: (conversationId: number) => void;
  onStartConversation: () => void;
  onSendMessage: () => void;
  onArchiveConversation: (conversationId: number) => void;
};

export function AssistantPanel({
  assistantStatus,
  conversations,
  selectedConversation,
  draftMessage,
  isLoadingAssistant,
  isSendingMessage,
  assistantError,
  onDraftMessageChange,
  onSelectConversation,
  onStartConversation,
  onSendMessage,
  onArchiveConversation,
}: AssistantPanelProps) {
  const assistantDisabled = assistantStatus !== null && !assistantStatus.enabled;

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSendMessage();
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

      <div className="assistant-layout">
        <aside className="assistant-conversations">
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

              <form className="assistant-composer" onSubmit={handleSubmit}>
                <textarea
                  value={draftMessage}
                  onChange={(event) => onDraftMessageChange(event.target.value)}
                  placeholder="Escribe tu mensaje…"
                  rows={3}
                  disabled={isSendingMessage || assistantDisabled}
                />
                <button
                  type="submit"
                  disabled={
                    isSendingMessage ||
                    assistantDisabled ||
                    draftMessage.trim().length === 0
                  }
                >
                  {isSendingMessage ? "Enviando…" : "Enviar"}
                </button>
              </form>
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
