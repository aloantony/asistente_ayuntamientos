import {
  Brain,
  CheckCircle2,
  CircleAlert,
  Clock3,
  Database,
  FileText,
  Globe2,
  GripVertical,
  Hammer,
  Inbox,
  Loader2,
  MapPin,
  PanelLeftClose,
  PanelLeftOpen,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
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
  formatAssistantTool,
  type AssistantAction,
  type AssistantConversation,
  type AssistantConversationDetail,
  type AssistantConversationFolder,
  type AssistantStatus,
  type User,
} from "./types";
import { createSilenceDetector } from "../lib/voice";

type AssistantPanelProps = {
  assistantStatus: AssistantStatus | null;
  conversations: AssistantConversation[];
  conversationFolders: AssistantConversationFolder[];
  currentUser: User;
  selectedConversation: AssistantConversationDetail | null;
  draftMessage: string;
  voiceModeEnabled: boolean;
  handsFreeEnabled: boolean;
  isLoadingAssistant: boolean;
  isSendingMessage: boolean;
  isSpeaking: boolean;
  assistantError: string;
  includeArchivedConversations: boolean;
  onDraftMessageChange: (value: string) => void;
  onVoiceModeChange: (enabled: boolean) => void;
  onHandsFreeChange: (enabled: boolean) => void;
  onSelectConversation: (conversationId: number) => void;
  onStartConversation: () => void;
  onSendMessage: () => void;
  onSendVoiceTranscript: (transcript: string) => void;
  onStopSpeaking: () => void;
  onTranscribeAudio: (audio: Blob) => Promise<string>;
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

// Sugerencias rápidas del compositor: sólo prerrellenan el borrador, no envían.
const SUGGESTED_PROMPTS = [
  "Preparar un resumen ejecutivo",
  "Comparar dos ordenanzas",
  "Ordenar mis notas de trabajo",
];

const EMPTY_THREAD_MESSAGES = [
  {
    title: "Empecemos con calma",
    body:
      "Escribe tu consulta o tus notas. Anacleto te ayudará a ordenarlas y convertirlas en un trabajo claro.",
  },
  {
    title: "Cuéntame qué necesitas",
    body:
      "Puedes escribirlo como lo dirías en una reunión. Después lo convertimos juntos en una explicación ordenada.",
  },
  {
    title: "Pongamos orden a las ideas",
    body:
      "Trae una duda, un documento o unas notas sueltas. Anacleto te ayudará a preparar el siguiente paso.",
  },
  {
    title: "Vamos paso a paso",
    body:
      "No hace falta redactarlo perfecto. Empieza con lo importante y el asistente te ayudará a darle forma.",
  },
  {
    title: "Un buen comienzo basta",
    body:
      "Escribe una frase, una preocupación o una tarea pendiente. A partir de ahí podremos aclararla y trabajarla.",
  },
];

const NO_SELECTION_MESSAGES = [
  {
    title: "Elige una conversación",
    body:
      "También puedes empezar una nueva y contar, con tus propias palabras, qué necesitas revisar, preparar o recordar.",
  },
  {
    title: "Tu mesa de trabajo está lista",
    body:
      "Abre una conversación anterior o crea una nueva para seguir trabajando con tranquilidad.",
  },
  {
    title: "Aquí puedes retomar el hilo",
    body:
      "Selecciona una conversación de la lista o empieza una nueva consulta cuando quieras.",
  },
];

const UNCATEGORIZED_FOLDER_ID = "sin-carpeta";
function normalizeFolderName(value: string) {
  return value.trim().replace(/\s+/g, " ");
}

function stableMessageIndex(seed: number | string, length: number) {
  const text = String(seed);
  let hash = 0;
  for (let index = 0; index < text.length; index += 1) {
    hash = (hash * 31 + text.charCodeAt(index)) % length;
  }
  return hash;
}

type AssistantSymbolName =
  | "archive"
  | "conversation-list-close"
  | "conversation-list-open"
  | "copy"
  | "folder"
  | "mark"
  | "mic"
  | "more"
  | "new-chat"
  | "rename"
  | "restore"
  | "search"
  | "send";

function AssistantSymbolIcon({
  name,
  size = 16,
}: {
  name: AssistantSymbolName;
  size?: number;
}) {
  return (
    <svg aria-hidden="true" height={size} viewBox="0 0 24 24" width={size}>
      <use href={`/icons/assistant-symbols.svg#icon-assistant-${name}`} />
    </svg>
  );
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
    { id: "archived", label: "Archivadas", conversations: [] },
    { id: "today", label: "Hoy", conversations: [] },
    { id: "week", label: "Últimos 7 días", conversations: [] },
    { id: "older", label: "Anteriores", conversations: [] },
  ];

  for (const conversation of conversations) {
    if (conversation.status === "archived") {
      groups[0].conversations.push(conversation);
      continue;
    }

    const ageInDays = daysBetweenNow(conversation.updated_at);
    if (ageInDays < 1) {
      groups[1].conversations.push(conversation);
    } else if (ageInDays < 7) {
      groups[2].conversations.push(conversation);
    } else {
      groups[3].conversations.push(conversation);
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
  if (tool === "get_map_items") {
    return MapPin;
  }
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
  if (action.status === "started") {
    return "En curso.";
  }

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

function getMapActionItems(actions: AssistantAction[]) {
  return actions.flatMap((action) => {
    if (action.tool !== "get_map_items" || !action.ok) {
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
      .filter((item): item is Record<string, unknown> => {
        return Boolean(
          item &&
            typeof item === "object" &&
            "map_url" in item &&
            item.map_url,
        );
      })
      .map((item) => ({
        title: String(item.title ?? "Ubicación"),
        label:
          item.location && typeof item.location === "object"
            ? String((item.location as { label?: unknown }).label ?? "Mapa municipal")
            : "Mapa municipal",
        url: String(item.map_url),
      }));
  });
}

function AssistantMapActions({ actions }: { actions: AssistantAction[] }) {
  const mapItems = getMapActionItems(actions);
  if (mapItems.length === 0) {
    return null;
  }
  return (
    <div className="assistant-map-actions" aria-label="Acciones de mapa">
      {mapItems.slice(0, 3).map((item) => (
        <a className="assistant-map-action-card" href={item.url} key={item.url}>
          <MapPin aria-hidden size={16} />
          <span>
            <strong>{item.title}</strong>
            <small>{item.label}</small>
          </span>
          <em>Ver en mapa</em>
        </a>
      ))}
    </div>
  );
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
          const isPending = action.status === "started";

          return (
            <div
              className={
                isPending
                  ? "assistant-action-event pending"
                  : action.ok
                  ? "assistant-action-event ok"
                  : "assistant-action-event error"
              }
              key={`${action.tool}-${index}`}
            >
              <div className="assistant-action-marker">
                {isPending ? (
                  <Loader2 aria-hidden className="spinning-icon" size={16} />
                ) : action.ok ? (
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
                {!isPending ? (
                  <details className="assistant-action-detail">
                    <summary>Detalle tecnico</summary>
                    <pre>{actionDetailText(action)}</pre>
                  </details>
                ) : null}
              </div>
            </div>
          );
        })}
      </div>
    </details>
  );
}

function AssistantMarkdown({ content }: { content: string }) {
  return (
    <ReactMarkdown
      allowedElements={[
        "p",
        "strong",
        "em",
        "ul",
        "ol",
        "li",
        "blockquote",
        "code",
        "pre",
        "a",
        "h1",
        "h2",
        "h3",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "hr",
        "br",
      ]}
      components={{
        a: ({ href, children }) => (
          <a href={href} rel="noreferrer" target="_blank">
            {children}
          </a>
        ),
      }}
      remarkPlugins={[remarkGfm]}
    >
      {content}
    </ReactMarkdown>
  );
}

export function AssistantPanel({
  assistantStatus,
  conversations,
  conversationFolders,
  currentUser,
  selectedConversation,
  draftMessage,
  voiceModeEnabled,
  handsFreeEnabled,
  isLoadingAssistant,
  isSendingMessage,
  isSpeaking,
  assistantError,
  includeArchivedConversations,
  onDraftMessageChange,
  onVoiceModeChange,
  onHandsFreeChange,
  onSelectConversation,
  onStartConversation,
  onSendMessage,
  onSendVoiceTranscript,
  onStopSpeaking,
  onTranscribeAudio,
  onArchiveConversation,
  onRestoreConversation,
  onRenameConversation,
  onAssignConversationFolder,
  onCreateConversationFolder,
  onRenameConversationFolder,
  onDeleteConversationFolder,
  onIncludeArchivedConversationsChange,
}: AssistantPanelProps) {
  const assistantDisabled = assistantStatus !== null && !assistantStatus.enabled;
  const runtimeHealthFailed =
    assistantStatus?.runtime === "hermes_agent" &&
    assistantStatus.runtime_healthy === false &&
    assistantStatus.enabled;
  const selectedIsArchived = selectedConversation?.status === "archived";
  const composerDisabled =
    isSendingMessage || assistantDisabled || Boolean(selectedIsArchived);
  const emptyThreadMessage = useMemo(
    () =>
      EMPTY_THREAD_MESSAGES[
        stableMessageIndex(
          selectedConversation?.id ?? "sin-conversacion",
          EMPTY_THREAD_MESSAGES.length,
        )
      ],
    [selectedConversation?.id],
  );
  const noSelectionMessage = useMemo(
    () =>
      NO_SELECTION_MESSAGES[
        stableMessageIndex(
          currentUser.id,
          NO_SELECTION_MESSAGES.length,
        )
      ],
    [currentUser.id],
  );
  const toolLabels = useMemo(
    () =>
      Object.fromEntries(
        assistantStatus?.tools.map((tool) => [tool.name, tool.label]) ?? [],
      ),
    [assistantStatus],
  );
  const [inlineTitleValue, setInlineTitleValue] = useState("");
  const [isListening, setIsListening] = useState(false);
  const [isTranscribingVoice, setIsTranscribingVoice] = useState(false);
  const [voiceError, setVoiceError] = useState("");
  const [voiceLoopActive, setVoiceLoopActive] = useState(false);
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
  const conversationFolderById = useMemo(
    () => new Map(conversationFolders.map((folder) => [folder.id, folder])),
    [conversationFolders],
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
  const [copiedMessageId, setCopiedMessageId] = useState<number | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const silenceDetectorRef = useRef<ReturnType<typeof createSilenceDetector> | null>(
    null,
  );
  const discardNextAudioRef = useRef(false);
  const wasSpeakingRef = useRef(false);
  const audioChunksRef = useRef<Blob[]>([]);
  const draftMessageRef = useRef(draftMessage);
  const messageTextareaRef = useRef<HTMLTextAreaElement | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);
  const speechTranscriptionEnabled = Boolean(
    assistantStatus?.speech_transcription_enabled,
  );
  const voiceDialogueAvailable =
    speechTranscriptionEnabled &&
    Boolean(assistantStatus?.speech_synthesis_enabled) &&
    speechSupported;
  const voiceStatus = isListening
    ? "Escuchando…"
    : isTranscribingVoice
      ? "Transcribiendo…"
      : voiceModeEnabled && isSendingMessage
        ? "Pensando…"
        : isSpeaking
          ? "Hablando…"
          : "";

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
    messagesEndRef.current?.scrollIntoView({ block: "end" });
  }, [
    selectedConversation?.messages
      .map(
        (message) =>
          `${message.id}:${message.content.length}:${message.actions.length}`,
      )
      .join("|"),
    isSendingMessage,
  ]);

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
    if (!selectedConversation || composerDisabled) {
      return;
    }

    messageTextareaRef.current?.focus({ preventScroll: true });
  }, [selectedConversation?.id, composerDisabled]);

  useEffect(() => {
    setSpeechSupported(
      typeof navigator !== "undefined" &&
        Boolean(navigator.mediaDevices?.getUserMedia) &&
        typeof MediaRecorder !== "undefined",
    );
  }, []);

  function releaseAudioStream() {
    silenceDetectorRef.current?.stop();
    silenceDetectorRef.current = null;
    mediaStreamRef.current?.getTracks().forEach((track) => track.stop());
    mediaStreamRef.current = null;
  }

  function stopListening(options: { discardAudio?: boolean } = {}) {
    const recorder = mediaRecorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      if (options.discardAudio) {
        discardNextAudioRef.current = true;
      }
      recorder.stop();
      return;
    }
    discardNextAudioRef.current = false;
    mediaRecorderRef.current = null;
    releaseAudioStream();
    setIsListening(false);
  }

  function pauseVoiceLoop(discardAudio = true) {
    setVoiceLoopActive(false);
    stopListening({ discardAudio });
    onStopSpeaking();
  }

  useEffect(() => {
    return () => {
      const recorder = mediaRecorderRef.current;
      if (recorder && recorder.state !== "inactive") {
        recorder.stop();
      }
      releaseAudioStream();
    };
  }, []);

  useEffect(() => {
    if (!voiceModeEnabled || assistantError) {
      pauseVoiceLoop(true);
      return;
    }
    if (!handsFreeEnabled) {
      setVoiceLoopActive(false);
      silenceDetectorRef.current?.stop();
      silenceDetectorRef.current = null;
    }
  }, [assistantError, handsFreeEnabled, voiceModeEnabled]);

  useEffect(() => {
    function handleVisibilityChange() {
      if (document.visibilityState === "hidden") {
        pauseVoiceLoop(true);
      }
    }

    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, []);

  useEffect(() => {
    if (isSpeaking) {
      wasSpeakingRef.current = true;
      return;
    }

    if (!wasSpeakingRef.current) {
      return;
    }
    wasSpeakingRef.current = false;

    if (
      voiceLoopActive &&
      voiceModeEnabled &&
      handsFreeEnabled &&
      !isListening &&
      !isTranscribingVoice &&
      !isSendingMessage &&
      !composerDisabled &&
      document.visibilityState !== "hidden"
    ) {
      void startListening({ force: true, loop: true });
    }
  }, [
    composerDisabled,
    handsFreeEnabled,
    isListening,
    isSendingMessage,
    isSpeaking,
    isTranscribingVoice,
    voiceLoopActive,
    voiceModeEnabled,
  ]);

  async function appendTranscribedAudio(audio: Blob) {
    if (audio.size === 0) {
      return;
    }
    setIsTranscribingVoice(true);
    try {
      const transcript = (await onTranscribeAudio(audio)).trim();
      if (!transcript) {
        setVoiceError("No he detectado texto en el audio. Prueba con una nota un poco más clara.");
        return;
      }
      if (voiceModeEnabled) {
        if (handsFreeEnabled) {
          setVoiceLoopActive(true);
        }
        onSendVoiceTranscript(transcript);
        return;
      }
      const currentDraft = draftMessageRef.current;
      onDraftMessageChange(
        currentDraft ? `${currentDraft} ${transcript}` : transcript,
      );
      messageTextareaRef.current?.focus({ preventScroll: true });
    } catch (error) {
      setVoiceLoopActive(false);
      setVoiceError(
        error instanceof Error
          ? error.message
          : "No se pudo transcribir el audio. Inténtalo de nuevo.",
      );
    } finally {
      setIsTranscribingVoice(false);
    }
  }

  async function startListening(
    options: { force?: boolean; loop?: boolean } = {},
  ) {
    if (
      !speechSupported ||
      mediaRecorderRef.current ||
      (isSpeaking && !options.force)
    ) {
      return;
    }

    setVoiceError("");
    if (options.loop) {
      setVoiceLoopActive(true);
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const recorder = new MediaRecorder(stream);
      mediaStreamRef.current = stream;
      audioChunksRef.current = [];
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          audioChunksRef.current.push(event.data);
        }
      };
      recorder.onstop = () => {
        const audio = new Blob(audioChunksRef.current, {
          type: recorder.mimeType || "audio/webm",
        });
        const shouldDiscardAudio = discardNextAudioRef.current;
        discardNextAudioRef.current = false;
        audioChunksRef.current = [];
        mediaRecorderRef.current = null;
        releaseAudioStream();
        setIsListening(false);
        if (!shouldDiscardAudio) {
          void appendTranscribedAudio(audio);
        }
      };
      mediaRecorderRef.current = recorder;
      recorder.start();
      if (voiceModeEnabled && handsFreeEnabled) {
        silenceDetectorRef.current = createSilenceDetector(stream, {
          onSilence: () => stopListening(),
          onTimeout: () => {
            setVoiceError(
              "No he detectado voz. Pulsa el micrófono cuando quieras continuar.",
            );
            pauseVoiceLoop(true);
          },
        });
      }
      setIsListening(true);
    } catch {
      releaseAudioStream();
      mediaRecorderRef.current = null;
      setIsListening(false);
      setVoiceLoopActive(false);
      setVoiceError(
        "No se pudo usar el microfono. Revisa los permisos del navegador.",
      );
    }
  }

  function handleToggleListening() {
    if (isSpeaking && voiceModeEnabled && handsFreeEnabled) {
      onStopSpeaking();
      setVoiceLoopActive(true);
      void startListening({ force: true, loop: true });
      return;
    }
    if (isListening) {
      stopListening();
    } else {
      void startListening({
        loop: voiceModeEnabled && handsFreeEnabled,
      });
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
  }

  function findConversationFolderByName(name: string) {
    return conversationFolders.find(
      (folder) => folder.name.toLowerCase() === name.toLowerCase(),
    );
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

    const existing = findConversationFolderByName(name);
    const folder = existing ?? (await onCreateConversationFolder(name));
    if (!folder) {
      return;
    }
    const folderId = folder.id;
    await moveConversationToFolder(conversationId, folderId);
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
    const folder =
      findConversationFolderByName(nextFolderName) ??
      (await onCreateConversationFolder(nextFolderName));
    if (!folder) {
      return;
    }
    const folderId = folder.id;

    await Promise.all([
      onAssignConversationFolder(sourceConversation.id, folderId),
      onAssignConversationFolder(targetConversation.id, folderId),
    ]);
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
    if (conversationListMode !== "folders") {
      handleConversationDragEnd();
      return;
    }
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

  return (
    <section className="panel assistant-agent-panel">
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
          <span>El asistente no responde ahora mismo.</span>
        </div>
      ) : null}

      <div
        className={[
          "assistant-agent-grid",
          isConversationListOpen ? "" : "conversations-collapsed",
        ]
          .filter(Boolean)
          .join(" ")}
      >
        {!isConversationListOpen ? (
          <div className="assistant-floating-actions">
            <button
              className="assistant-conversations-toggle assistant-conversations-toggle-floating"
              type="button"
              onClick={() => setIsConversationListOpen(true)}
              aria-expanded={false}
              aria-label="Desplegar lista de chats"
              title="Desplegar lista de chats"
            >
              <PanelLeftOpen aria-hidden size={18} />
              <span>Chats</span>
            </button>
            <button
              className="secondary-button assistant-new-chat assistant-new-chat-floating"
              type="button"
              onClick={onStartConversation}
              disabled={isLoadingAssistant || isSendingMessage || assistantDisabled}
              aria-label="Nueva conversación"
              title="Nueva conversación"
            >
              <AssistantSymbolIcon name="new-chat" size={17} />
              <span>Nueva conversacion</span>
            </button>
          </div>
        ) : null}

        {isConversationListOpen ? (
        <aside className="assistant-conversations">
          <div className="assistant-conversations-head">
            <button
              className="assistant-conversations-toggle"
              type="button"
              onClick={() => setIsConversationListOpen(false)}
              aria-expanded={true}
              aria-label="Plegar lista de chats"
              title="Plegar lista de chats"
            >
              <PanelLeftClose aria-hidden size={18} />
              <span>Chats</span>
            </button>
            <button
              className="secondary-button assistant-new-chat assistant-new-chat-compact"
              type="button"
              onClick={onStartConversation}
              disabled={isLoadingAssistant || isSendingMessage || assistantDisabled}
              aria-label="Nueva conversación"
              title="Nueva conversación"
            >
              <AssistantSymbolIcon name="new-chat" size={17} />
              <span>Nueva conversacion</span>
            </button>
          </div>

          <div className="assistant-list-tools">
                <div className="assistant-search">
                  <AssistantSymbolIcon name="search" size={16} />
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
                      <AssistantSymbolIcon name="folder" size={13} />
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
                    <AssistantSymbolIcon name="archive" size={13} />
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
                  const isFolderView = conversationListMode === "folders";
                  const folderGroupId = group.id as ConversationFolderId;
                  const groupIcon = isFolderView ? "folder" : null;

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
                      onDragOver={
                        isFolderView
                          ? (event) =>
                              handleConversationDragOverFolder(event, folderGroupId)
                          : undefined
                      }
                      onDrop={
                        isFolderView
                          ? (event) =>
                              handleConversationDropOnFolder(event, folderGroupId)
                          : undefined
                      }
                    >
                      <summary
                        onContextMenu={
                          isFolderView
                            ? (event) => openFolderContextMenu(event, folderGroupId)
                            : undefined
                        }
                      >
                        {groupIcon ? (
                          <AssistantSymbolIcon name={groupIcon} size={14} />
                        ) : (
                          <Clock3 aria-hidden size={14} />
                        )}
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
                          const folder =
                            typeof folderId === "number"
                              ? conversationFolderById.get(folderId)
                              : undefined;

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
                                      {folder ? (
                                        <span
                                          className="assistant-folder-pill"
                                          title={`Carpeta: ${folder.name}`}
                                        >
                                          <AssistantSymbolIcon name="folder" size={11} />
                                          <span>{folder.name}</span>
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
                                  <AssistantSymbolIcon name="more" size={15} />
                                </button>
                                {conversationListMode === "folders" ? (
                                  <span className="assistant-drop-hint">
                                    <AssistantSymbolIcon name="folder" size={14} />
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
        </aside>
        ) : null}

        <main
          className={[
            "assistant-thread",
            selectedConversation ? "" : "assistant-thread-empty",
            selectedConversation?.messages.length === 0
              ? "assistant-thread-new"
              : "",
          ]
            .filter(Boolean)
            .join(" ")}
        >
          {selectedConversation ? (
            <>
              <div className="assistant-thread-actions">
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
                    <AssistantSymbolIcon name="restore" size={16} />
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
                    <AssistantSymbolIcon name="archive" size={16} />
                  </button>
                )}
              </div>

              <div className="assistant-messages">
                {selectedConversation.messages.length === 0 ? (
                  <div className="assistant-empty-thread">
                    <h3>{emptyThreadMessage.title}</h3>
                    <p>{emptyThreadMessage.body}</p>
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
                          <AssistantSymbolIcon name="mark" size={16} />
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
                          {isAssistant ? (
                            <div className="assistant-message-content markdown-content">
                              <AssistantMarkdown content={message.content} />
                            </div>
                          ) : (
                            <p className="assistant-message-content">
                              {message.content}
                            </p>
                          )}
                        </div>
                        <AssistantMapActions actions={message.actions} />
                        {message.actions.length > 0 ? (
                          <ActionTimeline
                            actions={message.actions}
                            toolLabels={toolLabels}
                          />
                        ) : null}
                        {isAssistant && message.content.trim().length > 0 ? (
                          <div className="assistant-message-actions">
                            <button
                              type="button"
                              className="assistant-msg-action"
                              onClick={() =>
                                handleCopyMessage(message.id, message.content)
                              }
                            >
                              <AssistantSymbolIcon name="copy" size={13} />
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
                {isSendingMessage &&
                !selectedConversation.messages.some(
                  (message) =>
                    message.id === -2 &&
                    (message.content.trim().length > 0 ||
                      message.actions.length > 0),
                ) ? (
                  <article className="assistant-message assistant working">
                    <div className="assistant-message-avatar">
                      <AssistantSymbolIcon name="mark" size={16} />
                    </div>
                    <div className="assistant-message-main">
                      <div
                        className="assistant-message-bubble assistant-typing-bubble"
                        aria-label="Anacleto está respondiendo"
                        role="status"
                      >
                        <span className="assistant-typing-indicator" aria-hidden="true">
                          <span />
                          <span />
                          <span />
                        </span>
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
                    <div className="assistant-composer-context" aria-hidden="true" />
                    <div className="assistant-composer-actions">
                      {voiceDialogueAvailable ? (
                        <button
                          type="button"
                          className={
                            voiceModeEnabled
                              ? "voice-mode-toggle active"
                              : "voice-mode-toggle"
                          }
                          aria-pressed={voiceModeEnabled}
                          onClick={() => onVoiceModeChange(!voiceModeEnabled)}
                          disabled={composerDisabled}
                        >
                          {voiceModeEnabled ? "Modo voz activado" : "Modo voz"}
                        </button>
                      ) : null}
                      {voiceDialogueAvailable && voiceModeEnabled ? (
                        <button
                          type="button"
                          className={
                            handsFreeEnabled
                              ? "voice-handsfree-toggle active"
                              : "voice-handsfree-toggle"
                          }
                          aria-pressed={handsFreeEnabled}
                          onClick={() => onHandsFreeChange(!handsFreeEnabled)}
                          disabled={composerDisabled}
                        >
                          Autoescucha
                        </button>
                      ) : null}
                      {speechTranscriptionEnabled ? (
                        <button
                          type="button"
                          className={
                            isListening
                              ? "assistant-mic recording"
                              : "assistant-mic"
                          }
                          aria-label={
                            isListening ? "Detener grabación" : "Grabar audio"
                          }
                          aria-pressed={isListening}
                          onClick={handleToggleListening}
                          disabled={
                            !speechSupported ||
                            composerDisabled ||
                            isTranscribingVoice ||
                            (isSpeaking && !(voiceModeEnabled && handsFreeEnabled))
                          }
                          title={
                            speechSupported
                              ? "Grabar audio y transcribirlo con Anacleto"
                              : "Grabación de audio no disponible en este navegador"
                          }
                        >
                          {isTranscribingVoice ? (
                            <Loader2 aria-hidden size={18} />
                          ) : isListening ? (
                            <AssistantSymbolIcon name="mic" size={18} />
                          ) : (
                            <AssistantSymbolIcon name="mic" size={18} />
                          )}
                        </button>
                      ) : null}
                      <button
                        type="submit"
                        disabled={
                          composerDisabled || draftMessage.trim().length === 0
                        }
                      >
                        <AssistantSymbolIcon name="send" size={17} />
                        <span>{isSendingMessage ? "Enviando" : "Enviar"}</span>
                      </button>
                    </div>
                  </div>
                </form>

                {voiceStatus ? (
                  <div className="voice-status" role="status">
                    <span>{voiceStatus}</span>
                    {isSpeaking ? (
                      <button
                        type="button"
                        className="voice-stop-button"
                        onClick={onStopSpeaking}
                      >
                        Detener voz
                      </button>
                    ) : null}
                  </div>
                ) : null}

                {voiceError ? (
                  <p className="error-message assistant-voice-error">
                    {voiceError}
                  </p>
                ) : null}
              </div>
            </>
          ) : (
            <div className="assistant-no-selection">
              <h3>
                {isLoadingAssistant
                  ? "Cargando conversaciones"
                  : noSelectionMessage.title}
              </h3>
              <p className="muted">
                {isLoadingAssistant
                  ? "Estamos preparando el historial y el estado del asistente."
                  : noSelectionMessage.body}
              </p>
              <button
                type="button"
                onClick={onStartConversation}
                disabled={
                  isLoadingAssistant || isSendingMessage || assistantDisabled
                }
              >
                <AssistantSymbolIcon name="new-chat" size={17} />
                <span>Nueva conversacion</span>
              </button>
            </div>
          )}
        </main>

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
                    <AssistantSymbolIcon name="new-chat" size={15} />
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
                    <AssistantSymbolIcon name="rename" size={15} />
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
                        <AssistantSymbolIcon name="folder" size={15} />
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
                    <AssistantSymbolIcon name="folder" size={15} />
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
                    <AssistantSymbolIcon name="copy" size={15} />
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
                      <AssistantSymbolIcon name="restore" size={15} />
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
                      <AssistantSymbolIcon name="archive" size={15} />
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
                    <AssistantSymbolIcon name="folder" size={15} />
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
                      <AssistantSymbolIcon name="rename" size={15} />
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
                    <AssistantSymbolIcon name="archive" size={15} />
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
