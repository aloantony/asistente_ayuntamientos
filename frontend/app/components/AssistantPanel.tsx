import {
  Archive,
  ArchiveRestore,
  Bot,
  Brain,
  CheckCircle2,
  ChevronDown,
  CircleAlert,
  Clock3,
  Copy,
  Database,
  FileText,
  Folder,
  FolderPlus,
  Globe2,
  GripVertical,
  Hammer,
  Inbox,
  Loader2,
  MessageSquarePlus,
  Mic,
  MicOff,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  Pencil,
  RotateCcw,
  Search,
  Send,
  ShieldCheck,
  Sparkles,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import {
  type DragEvent,
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
  type AssistantConversationFolder,
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
  conversationFolders: AssistantConversationFolder[];
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
  onRenameConversation: (conversationId: number, title: string) => Promise<void>;
  onAssignConversationFolder: (
    conversationId: number,
    folderId: number | null,
  ) => Promise<void>;
  onCreateConversationFolder: (
    name: string,
  ) => Promise<AssistantConversationFolder | null>;
  onRenameConversationFolder: (folderId: number, name: string) => Promise<void>;
  onDeleteConversationFolder: (folderId: number) => Promise<void>;
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

type ConversationListMode = "recent" | "folders";

type ConversationFolderId = number | typeof UNCATEGORIZED_FOLDER_ID;

type ConversationGroup = {
  id: ConversationFolderId | string;
  label: string;
  conversations: AssistantConversation[];
};

type ConversationDragTarget =
  | { type: "conversation"; id: number }
  | { type: "folder"; id: ConversationFolderId | string }
  | null;

type ConversationContextMenu =
  | { type: "conversation"; conversationId: number; x: number; y: number }
  | { type: "folder"; folderId: ConversationFolderId; x: number; y: number }
  | null;

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

// Sugerencias rápidas del compositor: sólo prerrellenan el borrador, no envían.
const SUGGESTED_PROMPTS = [
  "Preparar un resumen ejecutivo",
  "Comparar dos ordenanzas",
  "Ordenar mis notas de trabajo",
];

const UNCATEGORIZED_FOLDER_ID = "sin-carpeta";
function normalizeFolderName(value: string) {
  return value.trim().replace(/\s+/g, " ");
}

function daysBetweenNow(value: string) {
  const updatedAt = new Date(value).getTime();
  if (Number.isNaN(updatedAt)) {
    return Number.POSITIVE_INFINITY;
  }
  return (Date.now() - updatedAt) / 86_400_000;
}

function buildRecentConversationGroups(
  conversations: AssistantConversation[],
): ConversationGroup[] {
  const groups: ConversationGroup[] = [
    { id: "today", label: "Hoy", conversations: [] },
    { id: "week", label: "Últimos 7 días", conversations: [] },
    { id: "older", label: "Anteriores", conversations: [] },
    { id: "archived", label: "Archivadas", conversations: [] },
  ];

  for (const conversation of conversations) {
    if (conversation.status === "archived") {
      groups[3].conversations.push(conversation);
      continue;
    }

    const ageInDays = daysBetweenNow(conversation.updated_at);
    if (ageInDays < 1) {
      groups[0].conversations.push(conversation);
    } else if (ageInDays < 7) {
      groups[1].conversations.push(conversation);
    } else {
      groups[2].conversations.push(conversation);
    }
  }

  return groups.filter((group) => group.conversations.length > 0);
}

function buildFolderConversationGroups(
  conversations: AssistantConversation[],
  folders: AssistantConversationFolder[],
  conversationFolderMap: Record<number, ConversationFolderId>,
): ConversationGroup[] {
  const groups: ConversationGroup[] = [
    ...folders.map((folder) => ({
      id: folder.id,
      label: folder.name,
      conversations: [] as AssistantConversation[],
    })),
    { id: UNCATEGORIZED_FOLDER_ID, label: "Sin carpeta", conversations: [] },
  ];
  const groupById = new Map(groups.map((group) => [group.id, group]));

  for (const conversation of conversations) {
    const folderId = conversationFolderMap[conversation.id] ?? UNCATEGORIZED_FOLDER_ID;
    const group = groupById.get(folderId) ?? groupById.get(UNCATEGORIZED_FOLDER_ID);
    group?.conversations.push(conversation);
  }

  return groups.filter((group) => group.conversations.length > 0);
}

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
  conversationFolders,
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
  onRenameConversation,
  onAssignConversationFolder,
  onCreateConversationFolder,
  onRenameConversationFolder,
  onDeleteConversationFolder,
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
  const [isConversationListOpen, setIsConversationListOpen] = useState(true);
  const [conversationListMode, setConversationListMode] =
    useState<ConversationListMode>("recent");
  const conversationFolderMap = useMemo(
    () =>
      Object.fromEntries(
        conversations
          .filter((conversation) => conversation.folder_id !== null)
          .map((conversation) => [conversation.id, conversation.folder_id]),
      ) as Record<number, ConversationFolderId>,
    [conversations],
  );
  const [draggedConversationId, setDraggedConversationId] = useState<
    number | null
  >(null);
  const [conversationDragTarget, setConversationDragTarget] =
    useState<ConversationDragTarget>(null);
  const [conversationContextMenu, setConversationContextMenu] =
    useState<ConversationContextMenu>(null);
  const [lastCreatedFolderId, setLastCreatedFolderId] = useState<ConversationFolderId | null>(
    null,
  );
  const [isMemoryOpen, setIsMemoryOpen] = useState(memoryEntries.length > 0);
  const [isDetailsPanelOpen, setIsDetailsPanelOpen] = useState(false);
  const [copiedMessageId, setCopiedMessageId] = useState<number | null>(null);
  const [editingConversationTitleId, setEditingConversationTitleId] =
    useState<number | null>(null);
  const [conversationTitleDraft, setConversationTitleDraft] = useState("");
  const [isRenamingConversation, setIsRenamingConversation] = useState(false);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const draftMessageRef = useRef(draftMessage);
  const messageTextareaRef = useRef<HTMLTextAreaElement | null>(null);
  const titleInputRef = useRef<HTMLInputElement | null>(null);
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

  const conversationGroups = useMemo(() => {
    if (conversationListMode === "folders") {
      return buildFolderConversationGroups(
        filteredConversations,
        conversationFolders,
        conversationFolderMap,
      );
    }

    return buildRecentConversationGroups(filteredConversations);
  }, [
    conversationFolderMap,
    conversationFolders,
    conversationListMode,
    filteredConversations,
  ]);

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
    if (!conversationContextMenu) {
      return;
    }

    function closeMenu() {
      setConversationContextMenu(null);
    }

    function handleMenuKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") {
        closeMenu();
      }
    }

    window.addEventListener("click", closeMenu);
    window.addEventListener("contextmenu", closeMenu);
    window.addEventListener("keydown", handleMenuKeyDown);
    window.addEventListener("resize", closeMenu);
    window.addEventListener("scroll", closeMenu, true);

    return () => {
      window.removeEventListener("click", closeMenu);
      window.removeEventListener("contextmenu", closeMenu);
      window.removeEventListener("keydown", handleMenuKeyDown);
      window.removeEventListener("resize", closeMenu);
      window.removeEventListener("scroll", closeMenu, true);
    };
  }, [conversationContextMenu]);

  useEffect(() => {
    if (!selectedConversation) {
      setEditingConversationTitleId(null);
      setConversationTitleDraft("");
      return;
    }

    if (editingConversationTitleId !== selectedConversation.id) {
      setConversationTitleDraft(selectedConversation.title);
    }
  }, [editingConversationTitleId, selectedConversation]);

  useEffect(() => {
    if (editingConversationTitleId !== null) {
      titleInputRef.current?.focus({ preventScroll: true });
      titleInputRef.current?.select();
    }
  }, [editingConversationTitleId]);

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

  async function handleConversationFolderChange(
    conversationId: number,
    folderId: ConversationFolderId,
  ) {
    await onAssignConversationFolder(
      conversationId,
      folderId === UNCATEGORIZED_FOLDER_ID ? null : folderId,
    );
  }

  function openConversationContextMenu(
    event: MouseEvent<HTMLElement>,
    conversationId: number,
  ) {
    event.preventDefault();
    event.stopPropagation();
    setConversationContextMenu({
      type: "conversation",
      conversationId,
      x: event.clientX,
      y: event.clientY,
    });
  }

  function openFolderContextMenu(
    event: MouseEvent<HTMLElement>,
    folderId: ConversationFolderId,
  ) {
    if (conversationListMode !== "folders") {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    setConversationContextMenu({
      type: "folder",
      folderId,
      x: event.clientX,
      y: event.clientY,
    });
  }

  function closeConversationContextMenu() {
    setConversationContextMenu(null);
  }

  function getConversationFolderId(conversationId: number) {
    return conversationFolderMap[conversationId] ?? UNCATEGORIZED_FOLDER_ID;
  }

  async function moveConversationToFolder(
    conversationId: number,
    folderId: ConversationFolderId,
  ) {
    await handleConversationFolderChange(conversationId, folderId);
    setConversationListMode("folders");
  }


  async function renameConversationFromMenu(conversationId: number) {
    const conversation = conversations.find(
      (candidate) => candidate.id === conversationId,
    );
    if (!conversation) {
      return;
    }

    const nextTitle = window.prompt("Nuevo nombre de la conversación", conversation.title);
    const normalizedTitle = normalizeFolderName(nextTitle ?? "");
    if (!normalizedTitle || normalizedTitle === conversation.title) {
      return;
    }

    await onRenameConversation(conversationId, normalizedTitle);
  }

  async function createFolderForConversation(conversationId: number) {
    const conversation = conversations.find(
      (candidate) => candidate.id === conversationId,
    );
    if (!conversation) {
      return;
    }

    const suggestedName = normalizeFolderName(conversation.title).slice(0, 40);
    const name = normalizeFolderName(
      window.prompt("Nombre de la nueva carpeta", suggestedName || "Nueva carpeta") ?? "",
    );
    if (!name) {
      return;
    }

    const existing = conversationFolders.find(
      (folder) => folder.name.toLowerCase() === name.toLowerCase(),
    );
    const folder = existing ?? (await onCreateConversationFolder(name));
    if (!folder) {
      return;
    }
    const folderId = folder.id;
    await void moveConversationToFolder(conversationId, folderId);
    setLastCreatedFolderId(folderId);
    window.setTimeout(() => setLastCreatedFolderId(null), 1200);
  }

  async function renameFolderFromMenu(folderId: ConversationFolderId) {
    if (folderId === UNCATEGORIZED_FOLDER_ID) {
      return;
    }

    const folder = conversationFolders.find((candidate) => candidate.id === folderId);
    if (!folder) {
      return;
    }

    const nextName = normalizeFolderName(
      window.prompt("Nuevo nombre de la carpeta", folder.name) ?? "",
    );
    if (!nextName || nextName === folder.name) {
      return;
    }

    const nameExists = conversationFolders.some(
      (candidate) =>
        candidate.id !== folderId &&
        candidate.name.toLowerCase() === nextName.toLowerCase(),
    );
    if (nameExists) {
      window.alert("Ya existe una carpeta con ese nombre.");
      return;
    }

    await onRenameConversationFolder(folderId, nextName);
  }

  async function emptyFolder(folderId: ConversationFolderId) {
    const conversationsInFolder = conversations.filter(
      (conversation) => getConversationFolderId(conversation.id) === folderId,
    );
    await Promise.all(
      conversationsInFolder.map((conversation) =>
        onAssignConversationFolder(conversation.id, null),
      ),
    );
  }

  async function deleteFolder(folderId: ConversationFolderId) {
    if (folderId === UNCATEGORIZED_FOLDER_ID) {
      return;
    }

    const folder = conversationFolders.find((candidate) => candidate.id === folderId);
    if (!folder) {
      return;
    }

    if (
      !window.confirm(
        `Eliminar la carpeta "${folder.name}"? Las conversaciones quedarán en Sin carpeta.`,
      )
    ) {
      return;
    }

    await onDeleteConversationFolder(folderId);
  }

  function archiveFolderConversations(folderId: ConversationFolderId) {
    const conversationsInFolder = conversations.filter((conversation) => {
      const assignedFolderId = getConversationFolderId(conversation.id);
      return assignedFolderId === folderId && conversation.status !== "archived";
    });
    if (conversationsInFolder.length === 0) {
      return;
    }

    if (
      !window.confirm(
        `Archivar ${conversationsInFolder.length} conversaciones de esta carpeta?`,
      )
    ) {
      return;
    }

    for (const conversation of conversationsInFolder) {
      onArchiveConversation(conversation.id);
    }
  }

  function copyConversationLink(conversationId: number) {
    if (!navigator.clipboard) {
      return;
    }

    const url = new URL("/asistente", window.location.origin);
    url.searchParams.set("c", String(conversationId));
    void navigator.clipboard.writeText(url.toString());
  }

  async function createFolderFromConversationPair(
    sourceConversationId: number,
    targetConversationId: number,
  ) {
    if (sourceConversationId === targetConversationId) {
      return;
    }

    const sourceConversation = conversations.find(
      (conversation) => conversation.id === sourceConversationId,
    );
    const targetConversation = conversations.find(
      (conversation) => conversation.id === targetConversationId,
    );
    if (!sourceConversation || !targetConversation) {
      return;
    }

    const targetFolderId = conversationFolderMap[targetConversationId];
    if (targetFolderId && targetFolderId !== UNCATEGORIZED_FOLDER_ID) {
      void moveConversationToFolder(sourceConversationId, targetFolderId);
      setLastCreatedFolderId(targetFolderId);
      window.setTimeout(() => setLastCreatedFolderId(null), 1200);
      return;
    }

    const folderName = normalizeFolderName(targetConversation.title).slice(0, 40);
    const nextFolderName = folderName || "Nueva carpeta";
    const folder = await onCreateConversationFolder(nextFolderName);
    if (!folder) {
      return;
    }
    const folderId = folder.id;

    await Promise.all([
      onAssignConversationFolder(sourceConversation.id, folderId),
      onAssignConversationFolder(targetConversation.id, folderId),
    ]);
    setConversationListMode("folders");
    setLastCreatedFolderId(folderId);
    window.setTimeout(() => setLastCreatedFolderId(null), 1200);
  }

  function handleConversationDragStart(
    event: DragEvent<HTMLDivElement>,
    conversationId: number,
  ) {
    setDraggedConversationId(conversationId);
    setConversationDragTarget(null);
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", String(conversationId));
  }

  function handleConversationDragEnd() {
    setDraggedConversationId(null);
    setConversationDragTarget(null);
  }

  function handleConversationDragOverFolder(
    event: DragEvent<HTMLElement>,
    folderId: ConversationFolderId,
  ) {
    if (draggedConversationId === null || conversationListMode !== "folders") {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = "move";
    setConversationDragTarget({ type: "folder", id: folderId });
  }

  function handleConversationDropOnFolder(
    event: DragEvent<HTMLElement>,
    folderId: ConversationFolderId,
  ) {
    event.preventDefault();
    event.stopPropagation();
    const conversationId = draggedConversationId;
    handleConversationDragEnd();
    if (conversationId === null) {
      return;
    }
    void moveConversationToFolder(conversationId, folderId);
  }

  function handleConversationDragOverConversation(
    event: DragEvent<HTMLDivElement>,
    targetConversationId: number,
  ) {
    if (
      draggedConversationId === null ||
      draggedConversationId === targetConversationId
    ) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = "move";
    setConversationDragTarget({ type: "conversation", id: targetConversationId });
  }

  function handleConversationDropOnConversation(
    event: DragEvent<HTMLDivElement>,
    targetConversationId: number,
  ) {
    event.preventDefault();
    event.stopPropagation();
    const sourceConversationId = draggedConversationId;
    handleConversationDragEnd();
    if (sourceConversationId === null) {
      return;
    }
    void createFolderFromConversationPair(sourceConversationId, targetConversationId);
  }

  function startTitleEdit() {
    if (!selectedConversation || isRenamingConversation) {
      return;
    }
    setConversationTitleDraft(selectedConversation.title);
    setEditingConversationTitleId(selectedConversation.id);
  }

  function cancelTitleEdit() {
    setEditingConversationTitleId(null);
    setConversationTitleDraft(selectedConversation?.title ?? "");
  }

  async function submitTitleEdit() {
    if (!selectedConversation || isRenamingConversation) {
      return;
    }

    const nextTitle = conversationTitleDraft.trim();
    if (!nextTitle) {
      cancelTitleEdit();
      return;
    }
    if (nextTitle === selectedConversation.title) {
      setEditingConversationTitleId(null);
      return;
    }

    setIsRenamingConversation(true);
    try {
      await onRenameConversation(selectedConversation.id, nextTitle);
      setEditingConversationTitleId(null);
    } catch {
      titleInputRef.current?.focus({ preventScroll: true });
    } finally {
      setIsRenamingConversation(false);
    }
  }

  function handleTitleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") {
      event.preventDefault();
      void submitTitleEdit();
    } else if (event.key === "Escape") {
      event.preventDefault();
      cancelTitleEdit();
    }
  }

  function handleCopyMessage(messageId: number, content: string) {
    if (!navigator.clipboard) {
      return;
    }
    navigator.clipboard
      .writeText(content)
      .then(() => {
        setCopiedMessageId(messageId);
        window.setTimeout(() => {
          setCopiedMessageId((current) =>
            current === messageId ? null : current,
          );
        }, 1500);
      })
      .catch(() => undefined);
  }

  // Estado de la barra de privacidad: refleja la salud real del runtime.
  const anacletoStatusLabel = assistantDisabled
    ? "Sin configurar"
    : runtimeHealthFailed
      ? "Revisar"
      : "En línea";
  const ConversationToggleIcon = isConversationListOpen
    ? PanelLeftClose
    : PanelLeftOpen;

  return (
    <section className="panel assistant-agent-panel">
      <div className="assistant-agent-header">
        <div className="assistant-agent-title">
          <p className="eyebrow">Asistente</p>
          <h2>Agente municipal</h2>
          <p className="muted">
            Conversa con Anacleto para consultar, estructurar trabajo y preparar
            borradores supervisados.
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

      <div className="assistant-banner">
        <span className="assistant-banner-icon">
          <ShieldCheck aria-hidden size={15} />
        </span>
        <p>
          <strong>Privacy/AI Gateway activo.</strong> Los datos se
          pseudonimizan antes de cualquier llamada externa. La IA asiste, no
          decide.
        </p>
        <span
          className={
            anacletoStatusLabel === "En línea"
              ? "assistant-banner-status online"
              : "assistant-banner-status"
          }
        >
          {anacletoStatusLabel}
        </span>
      </div>

      <div
        className={[
          "assistant-agent-grid",
          isDetailsPanelOpen ? "details-open" : "",
          isConversationListOpen ? "" : "conversations-collapsed",
        ]
          .filter(Boolean)
          .join(" ")}
      >
        <aside
          className={
            isConversationListOpen
              ? "assistant-conversations"
              : "assistant-conversations collapsed"
          }
        >
          <button
            className="assistant-conversations-toggle"
            type="button"
            onClick={() => setIsConversationListOpen((open) => !open)}
            aria-expanded={isConversationListOpen}
            aria-label={
              isConversationListOpen
                ? "Plegar lista de chats"
                : "Desplegar lista de chats"
            }
            title={
              isConversationListOpen
                ? "Plegar lista de chats"
                : "Desplegar lista de chats"
            }
          >
            <ConversationToggleIcon aria-hidden size={18} strokeWidth={1.8} />
            <span>Chats</span>
          </button>

          {isConversationListOpen ? (
            <>
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
                <div className="assistant-list-options">
                  <button
                    className="assistant-list-mode-toggle"
                    type="button"
                    aria-pressed={conversationListMode === "folders"}
                    aria-label={
                      conversationListMode === "folders"
                        ? "Cambiar a vista recientes"
                        : "Cambiar a vista por carpetas"
                    }
                    title={
                      conversationListMode === "folders"
                        ? "Cambiar a vista recientes"
                        : "Cambiar a vista por carpetas"
                    }
                    onClick={() =>
                      setConversationListMode((mode) =>
                        mode === "folders" ? "recent" : "folders",
                      )
                    }
                    disabled={isLoadingAssistant}
                  >
                    {conversationListMode === "folders" ? (
                      <Folder aria-hidden size={13} />
                    ) : (
                      <Clock3 aria-hidden size={13} />
                    )}
                    <span>
                      {conversationListMode === "folders"
                        ? "Carpetas"
                        : "Recientes"}
                    </span>
                  </button>
                  <button
                    className="assistant-archived-toggle"
                    type="button"
                    aria-pressed={includeArchivedConversations}
                    onClick={() =>
                      onIncludeArchivedConversationsChange(
                        !includeArchivedConversations,
                      )
                    }
                    disabled={isLoadingAssistant || isSendingMessage}
                  >
                    <Archive aria-hidden size={13} />
                    Archivadas
                  </button>
                </div>
              </div>

              {isLoadingAssistant ? (
                <p className="muted assistant-empty-state">Cargando conversaciones...</p>
              ) : null}
              {!isLoadingAssistant && filteredConversations.length === 0 ? (
                <p className="muted assistant-empty-state">
                  No hay conversaciones que mostrar.
                </p>
              ) : null}
              {conversationListMode === "folders" ? (
                <p className="assistant-drag-helper">
                  Arrastra un chat a una carpeta para moverlo, o encima de otro
                  chat para crear una carpeta con ambos.
                </p>
              ) : null}
              <div className="assistant-conversation-groups">
                {conversationGroups.map((group) => {
                  const isFolderDropTarget =
                    conversationDragTarget?.type === "folder" &&
                    conversationDragTarget.id === group.id;
                  const wasJustCreated = lastCreatedFolderId === group.id;
                  const FolderIcon =
                    group.id === UNCATEGORIZED_FOLDER_ID ? Inbox : Folder;

                  return (
                    <details
                      className={[
                        "assistant-conversation-group",
                        isFolderDropTarget ? "drop-target" : "",
                        wasJustCreated ? "just-created" : "",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                      key={group.id}
                      open
                      onDragOver={(event) =>
                        handleConversationDragOverFolder(
                          event,
                          group.id as ConversationFolderId,
                        )
                      }
                      onDrop={(event) =>
                        handleConversationDropOnFolder(
                          event,
                          group.id as ConversationFolderId,
                        )
                      }
                    >
                      <summary
                        onContextMenu={(event) =>
                          openFolderContextMenu(
                            event,
                            group.id as ConversationFolderId,
                          )
                        }
                      >
                        <FolderIcon aria-hidden size={14} />
                        <span>{group.label}</span>
                        <small>{group.conversations.length}</small>
                      </summary>
                      <ul>
                        {group.conversations.map((conversation) => {
                          const isConversationDropTarget =
                            conversationDragTarget?.type === "conversation" &&
                            conversationDragTarget.id === conversation.id;
                          const isDragging = draggedConversationId === conversation.id;
                          const folderId =
                            conversationFolderMap[conversation.id] ??
                            UNCATEGORIZED_FOLDER_ID;
                          const folder = conversationFolders.find(
                            (candidate) => candidate.id === folderId,
                          );

                          return (
                            <li key={conversation.id}>
                              <div
                                className={[
                                  "assistant-conversation-row",
                                  selectedConversation?.id === conversation.id
                                    ? "selected"
                                    : "",
                                  isDragging ? "dragging" : "",
                                  isConversationDropTarget ? "drop-target" : "",
                                ]
                                  .filter(Boolean)
                                  .join(" ")}
                                draggable={!isSendingMessage}
                                onDragStart={(event) =>
                                  handleConversationDragStart(event, conversation.id)
                                }
                                onDragEnd={handleConversationDragEnd}
                                onDragOver={(event) =>
                                  handleConversationDragOverConversation(
                                    event,
                                    conversation.id,
                                  )
                                }
                                onDrop={(event) =>
                                  handleConversationDropOnConversation(
                                    event,
                                    conversation.id,
                                  )
                                }
                                onContextMenu={(event) =>
                                  openConversationContextMenu(
                                    event,
                                    conversation.id,
                                  )
                                }
                              >
                                <button
                                  type="button"
                                  className="assistant-conversation-item"
                                  onClick={() => onSelectConversation(conversation.id)}
                                  disabled={isSendingMessage}
                                >
                                  <span className="assistant-conversation-drag-handle">
                                    <GripVertical aria-hidden size={14} />
                                  </span>
                                  <span className="assistant-conversation-copy">
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
                                      {conversationListMode === "folders" && folder ? (
                                        <span className="assistant-folder-pill">
                                          <Folder aria-hidden size={11} />
                                          {folder.name}
                                        </span>
                                      ) : null}
                                    </span>
                                  </span>
                                </button>
                                <button
                                  type="button"
                                  className="assistant-conversation-menu-button"
                                  aria-label={`Acciones de ${conversation.title}`}
                                  title="Acciones"
                                  disabled={isSendingMessage}
                                  onClick={(event) =>
                                    openConversationContextMenu(
                                      event,
                                      conversation.id,
                                    )
                                  }
                                >
                                  <MoreHorizontal aria-hidden size={15} />
                                </button>
                                {conversationListMode === "folders" ? (
                                  <span className="assistant-drop-hint">
                                    <FolderPlus aria-hidden size={14} />
                                    Suelta para agrupar
                                  </span>
                                ) : null}
                              </div>
                            </li>
                          );
                        })}
                      </ul>
                    </details>
                  );
                })}
              </div>
            </>
          ) : null}
        </aside>

        <main
          className={
            selectedConversation
              ? "assistant-thread"
              : "assistant-thread assistant-thread-empty"
          }
        >
          {selectedConversation ? (
            <>
              <div className="assistant-thread-header">
                <div className="assistant-thread-title-block">
                  {editingConversationTitleId === selectedConversation.id ? (
                    <input
                      ref={titleInputRef}
                      aria-label="Nombre de la conversación"
                      className="assistant-thread-title-input"
                      value={conversationTitleDraft}
                      maxLength={255}
                      disabled={isRenamingConversation}
                      onBlur={() => void submitTitleEdit()}
                      onChange={(event) =>
                        setConversationTitleDraft(event.target.value)
                      }
                      onKeyDown={handleTitleKeyDown}
                    />
                  ) : (
                    <button
                      type="button"
                      className="assistant-thread-title-button"
                      onClick={startTitleEdit}
                      title="Renombrar conversación"
                    >
                      <span>{selectedConversation.title}</span>
                      <small>Editar nombre</small>
                    </button>
                  )}
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
                    <p>Escribe el primer mensaje para empezar a trabajar.</p>
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
                          <Sparkles aria-hidden size={16} />
                        ) : (
                          <span>{currentUser.full_name.slice(0, 1)}</span>
                        )}
                      </div>
                      <div className="assistant-message-main">
                        <div className="assistant-message-meta">
                          <span>{isAssistant ? "Anacleto" : "Tu"}</span>
                          <small>{formatDate(message.created_at)}</small>
                        </div>
                        <div className="assistant-message-bubble">
                          <p className="assistant-message-content">
                            {message.content}
                          </p>
                        </div>
                        <ActionTimeline
                          actions={message.actions}
                          toolLabels={toolLabels}
                        />
                        {isAssistant && message.content.trim().length > 0 ? (
                          <div className="assistant-message-actions">
                            <button
                              type="button"
                              className="assistant-msg-action"
                              onClick={() =>
                                handleCopyMessage(message.id, message.content)
                              }
                            >
                              <Copy aria-hidden size={13} />
                              <span>
                                {copiedMessageId === message.id
                                  ? "Copiado"
                                  : "Copiar"}
                              </span>
                            </button>
                          </div>
                        ) : null}
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
                        <span>Anacleto</span>
                        <small>Trabajando</small>
                      </div>
                      <div className="assistant-message-bubble">
                        <p className="assistant-message-content muted">
                          Analizando la conversacion y herramientas
                          disponibles...
                        </p>
                      </div>
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

              <div className="assistant-composer-stack">
                {!composerDisabled ? (
                  <div className="assistant-chips">
                    {SUGGESTED_PROMPTS.map((prompt) => (
                      <button
                        key={prompt}
                        type="button"
                        className="assistant-chip"
                        onClick={() => {
                          onDraftMessageChange(prompt);
                          messageTextareaRef.current?.focus({
                            preventScroll: true,
                          });
                        }}
                      >
                        {prompt}
                      </button>
                    ))}
                  </div>
                ) : null}

                <form className="assistant-composer" onSubmit={handleSubmit}>
                  <textarea
                    ref={messageTextareaRef}
                    value={draftMessage}
                    onChange={(event) => onDraftMessageChange(event.target.value)}
                    onKeyDown={handleComposerKeyDown}
                    placeholder="Escribe tu consulta o pide un borrador…"
                    rows={3}
                    disabled={composerDisabled}
                  />
                  <div className="assistant-composer-foot">
                    <div className="assistant-composer-context">
                      <ShieldCheck aria-hidden size={13} />
                      <span>Contexto pseudonimizado · la IA asiste, no decide</span>
                    </div>
                    <div className="assistant-composer-actions">
                      <button
                        type="button"
                        className={
                          isListening
                            ? "assistant-mic recording"
                            : "assistant-mic"
                        }
                        aria-label={
                          isListening ? "Detener dictado" : "Iniciar dictado"
                        }
                        aria-pressed={isListening}
                        onClick={handleToggleListening}
                        disabled={!speechSupported || composerDisabled}
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
                  </div>
                </form>

                {voiceError ? (
                  <p className="error-message assistant-voice-error">
                    {voiceError}
                  </p>
                ) : null}
              </div>
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

      {conversationContextMenu ? (
        <div
          className="assistant-context-menu"
          role="menu"
          style={{
            left: conversationContextMenu.x,
            top: conversationContextMenu.y,
          }}
          onClick={(event) => event.stopPropagation()}
          onContextMenu={(event) => event.preventDefault()}
        >
          {conversationContextMenu.type === "conversation" ? (
            (() => {
              const conversation = conversations.find(
                (candidate) =>
                  candidate.id === conversationContextMenu.conversationId,
              );
              if (!conversation) {
                return null;
              }
              const currentFolderId = getConversationFolderId(conversation.id);
              return (
                <>
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      onSelectConversation(conversation.id);
                      closeConversationContextMenu();
                    }}
                  >
                    <MessageSquarePlus aria-hidden size={15} />
                    Abrir conversación
                  </button>
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      closeConversationContextMenu();
                      void renameConversationFromMenu(conversation.id);
                    }}
                  >
                    <Pencil aria-hidden size={15} />
                    Renombrar
                  </button>
                  <div className="assistant-context-menu-section">
                    <p>Mover a carpeta</p>
                    {conversationFolders.map((folder) => (
                      <button
                        key={folder.id}
                        type="button"
                        role="menuitem"
                        className={
                          currentFolderId === folder.id ? "selected" : undefined
                        }
                        onClick={() => {
                          moveConversationToFolder(conversation.id, folder.id);
                          closeConversationContextMenu();
                        }}
                      >
                        <Folder aria-hidden size={15} />
                        {folder.name}
                      </button>
                    ))}
                    <button
                      type="button"
                      role="menuitem"
                      className={
                        currentFolderId === UNCATEGORIZED_FOLDER_ID
                          ? "selected"
                          : undefined
                      }
                      onClick={() => {
                        moveConversationToFolder(
                          conversation.id,
                          UNCATEGORIZED_FOLDER_ID,
                        );
                        closeConversationContextMenu();
                      }}
                    >
                      <Inbox aria-hidden size={15} />
                      Sin carpeta
                    </button>
                  </div>
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      closeConversationContextMenu();
                      createFolderForConversation(conversation.id);
                    }}
                  >
                    <FolderPlus aria-hidden size={15} />
                    Nueva carpeta con este chat
                  </button>
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      copyConversationLink(conversation.id);
                      closeConversationContextMenu();
                    }}
                  >
                    <Copy aria-hidden size={15} />
                    Copiar enlace
                  </button>
                  {conversation.status === "archived" ? (
                    <button
                      type="button"
                      role="menuitem"
                      onClick={() => {
                        onRestoreConversation(conversation.id);
                        closeConversationContextMenu();
                      }}
                    >
                      <ArchiveRestore aria-hidden size={15} />
                      Restaurar
                    </button>
                  ) : (
                    <button
                      type="button"
                      role="menuitem"
                      className="danger"
                      onClick={() => {
                        if (
                          window.confirm(
                            "Archivar esta conversación? Dejará de aparecer en la lista.",
                          )
                        ) {
                          onArchiveConversation(conversation.id);
                        }
                        closeConversationContextMenu();
                      }}
                    >
                      <Archive aria-hidden size={15} />
                      Archivar
                    </button>
                  )}
                </>
              );
            })()
          ) : (
            (() => {
              const folder =
                conversationContextMenu.folderId === UNCATEGORIZED_FOLDER_ID
                  ? { id: UNCATEGORIZED_FOLDER_ID, name: "Sin carpeta" }
                  : conversationFolders.find(
                      (candidate) =>
                        candidate.id === conversationContextMenu.folderId,
                    );
              if (!folder) {
                return null;
              }
              const folderId = folder.id as ConversationFolderId;
              const isUncategorized = folder.id === UNCATEGORIZED_FOLDER_ID;
              return (
                <>
                  <div className="assistant-context-menu-title">
                    <Folder aria-hidden size={15} />
                    {folder.name}
                  </div>
                  {!isUncategorized ? (
                    <button
                      type="button"
                      role="menuitem"
                      onClick={() => {
                        closeConversationContextMenu();
                        renameFolderFromMenu(folderId);
                      }}
                    >
                      <Pencil aria-hidden size={15} />
                      Renombrar carpeta
                    </button>
                  ) : null}
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      emptyFolder(folderId);
                      closeConversationContextMenu();
                    }}
                  >
                    <Inbox aria-hidden size={15} />
                    Vaciar carpeta
                  </button>
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      archiveFolderConversations(folderId);
                      closeConversationContextMenu();
                    }}
                  >
                    <Archive aria-hidden size={15} />
                    Archivar conversaciones
                  </button>
                  {!isUncategorized ? (
                    <button
                      type="button"
                      role="menuitem"
                      className="danger"
                      onClick={() => {
                        closeConversationContextMenu();
                        deleteFolder(folderId);
                      }}
                    >
                      <XCircle aria-hidden size={15} />
                      Eliminar carpeta
                    </button>
                  ) : null}
                </>
              );
            })()
          )}
        </div>
      ) : null}
    </section>
  );
}
