import {
  Archive,
  ArchiveRestore,
  Bot,
  Brain,
  CheckCircle2,
  ChevronDown,
  CircleAlert,
  Clock3,
  Database,
  FileText,
  Globe2,
  Hammer,
  Loader2,
  MessageSquarePlus,
  Mic,
  MicOff,
  RotateCcw,
  Search,
  Send,
  ShieldCheck,
  Sparkles,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import {
  type FormEvent,
  type KeyboardEvent,
  type MouseEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ASSISTANT_MEMORY_CATEGORY_LABELS,
  ASSISTANT_MEMORY_SENSITIVITY_LABELS,
  formatAssistantTool,
  type AssistantAction,
  type AssistantConversation,
  type AssistantConversationDetail,
  type AssistantMemoryCategory,
  type AssistantMemoryEntry,
  type AssistantMemorySensitivity,
  type AssistantMemoryStatus,
  type AssistantStatus,
  type User,
  userHasPermission,
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

type AssistantPanelProps = {
  assistantStatus: AssistantStatus | null;
  conversations: AssistantConversation[];
  currentUser: User;
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

type Capability = {
  id: string;
  label: string;
  detail: string;
  enabled: boolean;
  icon: LucideIcon;
};

type ParsedActionResult = {
  data: unknown | null;
  text: string;
};

const REQUIREMENT_PERMISSIONS = [
  "requirements.view",
  "requirements.create",
  "requirements.edit",
];

const MEMORY_PERMISSIONS = [
  "assistant.memory.propose",
  "assistant.memory.view",
  "assistant.memory.review",
];

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

function userHasAnyPermission(user: User, permissions: string[]) {
  return permissions.some((permission) => userHasPermission(user, permission));
}

function buildCapabilities(
  currentUser: User,
  assistantStatus: AssistantStatus | null,
): Capability[] {
  const runtimeHealthy =
    assistantStatus?.enabled && assistantStatus.runtime_healthy !== false;

  return [
    {
      id: "chat",
      label: "Conversacion",
      detail: assistantStatus?.enabled ? "Disponible" : "Sin configurar",
      enabled: Boolean(assistantStatus?.enabled),
      icon: Bot,
    },
    {
      id: "requirements",
      label: "Necesidades",
      detail: userHasAnyPermission(currentUser, REQUIREMENT_PERMISSIONS)
        ? "Lectura y borradores"
        : "Sin permiso",
      enabled: userHasAnyPermission(currentUser, REQUIREMENT_PERMISSIONS),
      icon: FileText,
    },
    {
      id: "web",
      label: "Web",
      detail: userHasPermission(currentUser, "assistant.web.search")
        ? "Busqueda controlada"
        : "Sin permiso",
      enabled: userHasPermission(currentUser, "assistant.web.search"),
      icon: Globe2,
    },
    {
      id: "memory",
      label: "Memoria",
      detail: userHasAnyPermission(currentUser, MEMORY_PERMISSIONS)
        ? "Revision gobernada"
        : "Sin permiso",
      enabled: userHasAnyPermission(currentUser, MEMORY_PERMISSIONS),
      icon: Brain,
    },
    {
      id: "runtime",
      label: assistantStatus?.runtime ?? "Runtime",
      detail: !assistantStatus
        ? "Pendiente"
        : runtimeHealthy
          ? assistantStatus.model
          : "Revisar",
      enabled: Boolean(runtimeHealthy),
      icon: ShieldCheck,
    },
  ];
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("es-ES", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function formatShortDate(value: string) {
  return new Intl.DateTimeFormat("es-ES", {
    day: "2-digit",
    month: "short",
  }).format(new Date(value));
}

function parseActionResult(result: string): ParsedActionResult {
  try {
    return { data: JSON.parse(result), text: result };
  } catch {
    return { data: null, text: result };
  }
}

function getActionIcon(tool: string): LucideIcon {
  if (tool === "web_search") {
    return Globe2;
  }
  if (tool === "propose_memory_entry") {
    return Brain;
  }
  if (tool.includes("requirement")) {
    return FileText;
  }
  if (tool.startsWith("list_") || tool.startsWith("get_")) {
    return Database;
  }
  return Hammer;
}

function getActionSummary(action: AssistantAction) {
  if (!action.ok) {
    return "La herramienta devolvio un error.";
  }

  const parsed = parseActionResult(action.result);
  if (
    action.tool === "web_search" &&
    parsed.data &&
    typeof parsed.data === "object" &&
    "results" in parsed.data &&
    Array.isArray((parsed.data as { results?: unknown }).results)
  ) {
    const count = (parsed.data as { results: unknown[] }).results.length;
    return count === 1 ? "1 fuente localizada." : `${count} fuentes localizadas.`;
  }

  if (
    parsed.data &&
    typeof parsed.data === "object" &&
    "title" in parsed.data &&
    typeof (parsed.data as { title?: unknown }).title === "string"
  ) {
    return String((parsed.data as { title: string }).title);
  }

  return "Accion completada por el backend.";
}

function getWebResults(action: AssistantAction) {
  if (action.tool !== "web_search" || !action.ok) {
    return [];
  }

  const parsed = parseActionResult(action.result);
  if (
    !parsed.data ||
    typeof parsed.data !== "object" ||
    !("results" in parsed.data) ||
    !Array.isArray((parsed.data as { results?: unknown }).results)
  ) {
    return [];
  }

  return (parsed.data as { results: unknown[] }).results
    .filter((result): result is Record<string, unknown> => {
      return Boolean(result && typeof result === "object");
    })
    .map((result) => ({
      title: String(result.title ?? result.url ?? "Fuente"),
      url: String(result.url ?? ""),
      snippet: result.snippet ? String(result.snippet) : "",
      publishedAt: result.published_at ? String(result.published_at) : "",
    }))
    .filter((result) => result.url);
}

function actionDetailText(action: AssistantAction) {
  const input = JSON.stringify(action.input, null, 2);
  const parsed = parseActionResult(action.result);
  const result =
    parsed.data === null
      ? parsed.text
      : JSON.stringify(parsed.data, null, 2);
  return `Input\n${input}\n\nResultado\n${result}`;
}

function CapabilityStrip({
  capabilities,
}: {
  capabilities: Capability[];
}) {
  const enabledCount = capabilities.filter((capability) => capability.enabled).length;

  return (
    <details className="assistant-capabilities-panel">
      <summary>
        <span>Capacidades</span>
        <small>
          {enabledCount} de {capabilities.length} disponibles
        </small>
        <ChevronDown aria-hidden size={15} />
      </summary>
      <div className="assistant-capabilities" aria-label="Capacidades del asistente">
        {capabilities.map((capability) => {
          const Icon = capability.icon;
          return (
            <div
              className={
                capability.enabled
                  ? "assistant-capability enabled"
                  : "assistant-capability"
              }
              key={capability.id}
            >
              <Icon aria-hidden size={15} />
              <div>
                <span>{capability.label}</span>
                <small>{capability.detail}</small>
              </div>
            </div>
          );
        })}
      </div>
    </details>
  );
}

function ActionTimeline({
  actions,
  toolLabels,
}: {
  actions: AssistantAction[];
  toolLabels: Record<string, string>;
}) {
  if (actions.length === 0) {
    return null;
  }

  return (
    <details className="assistant-action-timeline">
      <summary>
        Actividad del asistente
        <span>{actions.length}</span>
      </summary>
      <div className="assistant-action-events">
        {actions.map((action, index) => {
          const Icon = getActionIcon(action.tool);
          const webResults = getWebResults(action);

          return (
            <div
              className={
                action.ok
                  ? "assistant-action-event ok"
                  : "assistant-action-event error"
              }
              key={`${action.tool}-${index}`}
            >
              <div className="assistant-action-marker">
                {action.ok ? (
                  <CheckCircle2 aria-hidden size={16} />
                ) : (
                  <XCircle aria-hidden size={16} />
                )}
              </div>
              <div className="assistant-action-body">
                <div className="assistant-action-title">
                  <Icon aria-hidden size={16} />
                  <span>{formatAssistantTool(action.tool, toolLabels)}</span>
                </div>
                <p>{getActionSummary(action)}</p>
                {webResults.length > 0 ? (
                  <div className="assistant-sources">
                    {webResults.map((result) => (
                      <a
                        href={result.url}
                        key={result.url}
                        rel="noreferrer"
                        target="_blank"
                      >
                        <span>{result.title}</span>
                        {result.snippet ? (
                          <small>{result.snippet}</small>
                        ) : null}
                      </a>
                    ))}
                  </div>
                ) : null}
                <details className="assistant-action-detail">
                  <summary>Detalle tecnico</summary>
                  <pre>{actionDetailText(action)}</pre>
                </details>
              </div>
            </div>
          );
        })}
      </div>
    </details>
  );
}

function MemoryReviewPanel({
  entries,
  isSendingMessage,
  onUpdateMemoryEntry,
}: {
  entries: AssistantMemoryEntry[];
  isSendingMessage: boolean;
  onUpdateMemoryEntry: AssistantPanelProps["onUpdateMemoryEntry"];
}) {
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

  if (entries.length === 0) {
    return <p className="muted">Sin propuestas pendientes.</p>;
  }

  return (
    <div className="assistant-memory-list">
      {entries.map((entry) => (
        <form
          className="assistant-memory-item"
          key={entry.id}
          onSubmit={(event) => handleMemoryEdit(event, entry.id)}
        >
          <textarea
            name="content"
            defaultValue={entry.content}
            rows={4}
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
            placeholder="Nota de revision"
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
  );
}

export function AssistantPanel({
  assistantStatus,
  conversations,
  currentUser,
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
  const runtimeHealthFailed =
    assistantStatus?.runtime === "hermes_agent" &&
    assistantStatus.runtime_healthy === false &&
    assistantStatus.enabled;
  const selectedIsArchived = selectedConversation?.status === "archived";
  const composerDisabled =
    isSendingMessage || assistantDisabled || Boolean(selectedIsArchived);
  const capabilities = useMemo(
    () => buildCapabilities(currentUser, assistantStatus),
    [assistantStatus, currentUser],
  );
  const toolLabels = useMemo(
    () =>
      Object.fromEntries(
        assistantStatus?.tools.map((tool) => [tool.name, tool.label]) ?? [],
      ),
    [assistantStatus],
  );

  const [isListening, setIsListening] = useState(false);
  const [voiceError, setVoiceError] = useState("");
  const [speechSupported, setSpeechSupported] = useState(false);
  const [conversationFilter, setConversationFilter] = useState("");
  const [isMemoryOpen, setIsMemoryOpen] = useState(memoryEntries.length > 0);
  const [isDetailsPanelOpen, setIsDetailsPanelOpen] = useState(false);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const draftMessageRef = useRef(draftMessage);
  const messageTextareaRef = useRef<HTMLTextAreaElement | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);

  const filteredConversations = useMemo(() => {
    const query = conversationFilter.trim().toLowerCase();
    if (!query) {
      return conversations;
    }
    return conversations.filter((conversation) =>
      conversation.title.toLowerCase().includes(query),
    );
  }, [conversationFilter, conversations]);

  useEffect(() => {
    draftMessageRef.current = draftMessage;
  }, [draftMessage]);

  useEffect(() => {
    if (memoryEntries.length > 0) {
      setIsMemoryOpen(true);
    }
  }, [memoryEntries.length]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ block: "end" });
  }, [selectedConversation?.messages.length, isSendingMessage]);

  useEffect(() => {
    if (!selectedConversation || composerDisabled) {
      return;
    }

    messageTextareaRef.current?.focus({ preventScroll: true });
  }, [selectedConversation?.id, composerDisabled]);

  useEffect(() => {
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
          "No se pudo usar el dictado por voz. Revisa los permisos del microfono.",
        );
      }
    };
    recognition.onend = () => {
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

  function handleComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) {
      return;
    }

    event.preventDefault();
    if (composerDisabled || draftMessage.trim().length === 0) {
      return;
    }

    stopListening();
    onSendMessage();
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    stopListening();
    onSendMessage();
  }

  return (
    <section className="panel assistant-agent-panel">
      <div className="assistant-agent-header">
        <div className="assistant-agent-title">
          <p className="eyebrow">Asistente</p>
          <h2>Agente municipal</h2>
          <p className="muted">
            Conversacion, necesidades, memoria y busqueda web gobernadas por
            permisos.
          </p>
        </div>
        <button
          className="secondary-button assistant-new-chat"
          type="button"
          onClick={onStartConversation}
          disabled={isLoadingAssistant || isSendingMessage || assistantDisabled}
        >
          <MessageSquarePlus aria-hidden size={17} />
          <span>Nueva conversacion</span>
        </button>
      </div>

      <CapabilityStrip capabilities={capabilities} />

      {assistantError ? (
        <div className="assistant-alert error-message">
          <CircleAlert aria-hidden size={18} />
          <span>{assistantError}</span>
        </div>
      ) : null}

      {assistantDisabled ? (
        <div className="assistant-alert muted">
          <CircleAlert aria-hidden size={18} />
          <span>El asistente no esta configurado en este servidor.</span>
        </div>
      ) : null}

      {runtimeHealthFailed ? (
        <div className="assistant-alert muted">
          <CircleAlert aria-hidden size={18} />
          <span>Hermes Agent esta configurado, pero su API no responde.</span>
        </div>
      ) : null}

      <div
        className={
          isDetailsPanelOpen
            ? "assistant-agent-grid details-open"
            : "assistant-agent-grid"
        }
      >
        <aside className="assistant-conversations">
          <div className="assistant-list-tools">
            <div className="assistant-search">
              <Search aria-hidden size={16} />
              <input
                aria-label="Buscar conversaciones"
                value={conversationFilter}
                onChange={(event) => setConversationFilter(event.target.value)}
                placeholder="Buscar"
                disabled={isLoadingAssistant}
              />
            </div>
            <label className="checkbox-label assistant-archived-toggle">
              <input
                checked={includeArchivedConversations}
                onChange={(event) =>
                  onIncludeArchivedConversationsChange(event.target.checked)
                }
                type="checkbox"
                disabled={isLoadingAssistant || isSendingMessage}
              />
              Archivadas
            </label>
          </div>

          {isLoadingAssistant ? (
            <p className="muted assistant-empty-state">Cargando conversaciones...</p>
          ) : null}
          {!isLoadingAssistant && filteredConversations.length === 0 ? (
            <p className="muted assistant-empty-state">
              No hay conversaciones que mostrar.
            </p>
          ) : null}
          <ul>
            {filteredConversations.map((conversation) => (
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
                  <span className="assistant-conversation-meta">
                    <Clock3 aria-hidden size={13} />
                    {formatShortDate(conversation.updated_at)}
                    {conversation.status === "archived" ? (
                      <span className="tag assistant-archived-tag">
                        Archivada
                      </span>
                    ) : null}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </aside>

        <main className="assistant-thread">
          {selectedConversation ? (
            <>
              <div className="assistant-thread-header">
                <div>
                  <h3>{selectedConversation.title}</h3>
                  {selectedIsArchived ? (
                    <span className="assistant-thread-status">Archivada</span>
                  ) : null}
                </div>
                {selectedIsArchived ? (
                  <button
                    className="secondary-button assistant-thread-icon-button"
                    type="button"
                    title="Restaurar conversacion"
                    aria-label="Restaurar conversacion"
                    disabled={isSendingMessage}
                    onClick={() =>
                      onRestoreConversation(selectedConversation.id)
                    }
                  >
                    <ArchiveRestore aria-hidden size={16} />
                  </button>
                ) : (
                  <button
                    className="danger-button assistant-thread-icon-button"
                    type="button"
                    title="Archivar conversacion"
                    aria-label="Archivar conversacion"
                    disabled={isSendingMessage}
                    onClick={() => {
                      if (
                        window.confirm(
                          "Archivar esta conversacion? Dejara de aparecer en la lista.",
                        )
                      ) {
                        onArchiveConversation(selectedConversation.id);
                      }
                    }}
                  >
                    <Archive aria-hidden size={16} />
                  </button>
                )}
              </div>

              <div className="assistant-messages">
                {selectedConversation.messages.length === 0 ? (
                  <div className="assistant-empty-thread">
                    <Sparkles aria-hidden size={22} />
                    <p>
                      Escribe el primer mensaje para iniciar la captura de
                      necesidades.
                    </p>
                  </div>
                ) : null}
                {selectedConversation.messages.map((message) => {
                  const isAssistant = message.role === "assistant";
                  return (
                    <article
                      key={message.id}
                      className={`assistant-message ${message.role}`}
                    >
                      <div className="assistant-message-avatar">
                        {isAssistant ? (
                          <Bot aria-hidden size={17} />
                        ) : (
                          <span>{currentUser.full_name.slice(0, 1)}</span>
                        )}
                      </div>
                      <div className="assistant-message-main">
                        <div className="assistant-message-meta">
                          <span>{isAssistant ? "Asistente" : "Tu"}</span>
                          <small>{formatDate(message.created_at)}</small>
                        </div>
                        <p className="assistant-message-content">
                          {message.content}
                        </p>
                        <ActionTimeline
                          actions={message.actions}
                          toolLabels={toolLabels}
                        />
                      </div>
                    </article>
                  );
                })}
                {isSendingMessage ? (
                  <article className="assistant-message assistant working">
                    <div className="assistant-message-avatar">
                      <Loader2 aria-hidden size={17} />
                    </div>
                    <div className="assistant-message-main">
                      <div className="assistant-message-meta">
                        <span>Asistente</span>
                        <small>Trabajando</small>
                      </div>
                      <p className="assistant-message-content muted">
                        Analizando la conversacion y herramientas disponibles...
                      </p>
                    </div>
                  </article>
                ) : null}
                <div ref={messagesEndRef} />
              </div>

              {selectedIsArchived ? (
                <p className="muted assistant-archived-note">
                  Esta conversacion esta archivada. Restaurala para seguir
                  escribiendo.
                </p>
              ) : null}

              <form className="assistant-composer" onSubmit={handleSubmit}>
                <textarea
                  ref={messageTextareaRef}
                  value={draftMessage}
                  onChange={(event) => onDraftMessageChange(event.target.value)}
                  onKeyDown={handleComposerKeyDown}
                  placeholder="Escribe tu mensaje..."
                  rows={3}
                  disabled={composerDisabled}
                />
                <div className="assistant-composer-actions">
                  <button
                    type="button"
                    className={
                      isListening ? "assistant-mic recording" : "assistant-mic"
                    }
                    aria-label={
                      isListening ? "Detener dictado" : "Iniciar dictado"
                    }
                    aria-pressed={isListening}
                    onClick={handleToggleListening}
                    disabled={
                      !speechSupported || composerDisabled
                    }
                    title={
                      speechSupported
                        ? "Dictado local en el dispositivo"
                        : "Dictado local no disponible en este navegador"
                    }
                  >
                    {isListening ? (
                      <MicOff aria-hidden size={18} />
                    ) : (
                      <Mic aria-hidden size={18} />
                    )}
                  </button>
                  <button
                    type="submit"
                    disabled={
                      composerDisabled || draftMessage.trim().length === 0
                    }
                  >
                    <Send aria-hidden size={17} />
                    <span>{isSendingMessage ? "Enviando" : "Enviar"}</span>
                  </button>
                </div>
              </form>

              {voiceError ? (
                <p className="error-message assistant-voice-error">
                  {voiceError}
                </p>
              ) : null}
            </>
          ) : (
            <div className="assistant-no-selection">
              <Bot aria-hidden size={28} />
              <h3>
                {isLoadingAssistant
                  ? "Cargando conversaciones"
                  : "Selecciona una conversacion"}
              </h3>
              <p className="muted">
                {isLoadingAssistant
                  ? "Estamos preparando el historial y el estado del asistente."
                  : "Tambien puedes crear una nueva para empezar desde cero."}
              </p>
              <button
                type="button"
                onClick={onStartConversation}
                disabled={
                  isLoadingAssistant || isSendingMessage || assistantDisabled
                }
              >
                <MessageSquarePlus aria-hidden size={17} />
                <span>Nueva conversacion</span>
              </button>
            </div>
          )}
        </main>

        <aside className="assistant-side-panel">
          <button
            className="assistant-details-toggle"
            type="button"
            onClick={() => setIsDetailsPanelOpen((open) => !open)}
            aria-expanded={isDetailsPanelOpen}
            aria-label={
              isDetailsPanelOpen
                ? "Ocultar detalles del asistente"
                : "Mostrar detalles del asistente"
            }
            title={
              isDetailsPanelOpen
                ? "Ocultar detalles del asistente"
                : "Mostrar detalles del asistente"
            }
          >
            <ShieldCheck aria-hidden size={17} />
            <span>Detalles</span>
            {memoryEntries.length > 0 ? (
              <small>{memoryEntries.length}</small>
            ) : null}
          </button>

          {isDetailsPanelOpen ? (
            <div className="assistant-side-content">
              <section className="assistant-side-section">
                <div className="assistant-side-heading">
                  <ShieldCheck aria-hidden size={17} />
                  <h3>Estado</h3>
                </div>
                <dl className="assistant-runtime-list">
                  <div>
                    <dt>Runtime</dt>
                    <dd>{assistantStatus?.runtime ?? "Pendiente"}</dd>
                  </div>
                  <div>
                    <dt>Modelo</dt>
                    <dd>{assistantStatus?.model ?? "Pendiente"}</dd>
                  </div>
                  <div>
                    <dt>Salud</dt>
                    <dd>
                      {!assistantStatus
                        ? "Pendiente"
                        : assistantStatus.runtime_healthy === false
                          ? "Revisar"
                          : "Operativo"}
                    </dd>
                  </div>
                  <div>
                    <dt>Planner</dt>
                    <dd>
                      {!assistantStatus
                        ? "Pendiente"
                        : assistantStatus.planner.enabled
                          ? assistantStatus.planner.model
                          : "Desactivado"}
                    </dd>
                  </div>
                </dl>
              </section>

              <section className="assistant-side-section">
                <button
                  className="assistant-memory-toggle"
                  type="button"
                  onClick={() => setIsMemoryOpen((open) => !open)}
                  aria-expanded={isMemoryOpen}
                >
                  <span>
                    <Brain aria-hidden size={17} />
                    Memoria pendiente
                  </span>
                  <span className="tag">{memoryEntries.length}</span>
                  <ChevronDown aria-hidden size={16} />
                </button>
                {isMemoryOpen ? (
                  <MemoryReviewPanel
                    entries={memoryEntries}
                    isSendingMessage={isSendingMessage}
                    onUpdateMemoryEntry={onUpdateMemoryEntry}
                  />
                ) : null}
              </section>

              <section className="assistant-side-section">
                <div className="assistant-side-heading">
                  <RotateCcw aria-hidden size={17} />
                  <h3>Actividad</h3>
                </div>
                <p className="muted">
                  {selectedConversation
                    ? `${selectedConversation.messages.length} mensajes`
                    : "Sin conversacion abierta"}
                </p>
              </section>
            </div>
          ) : null}
        </aside>
      </div>
    </section>
  );
}
