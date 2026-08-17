// Piezas de la conversación del asistente, extraídas de `AssistantPanel.tsx`
// (ADR-046). Son helpers puros y componentes de presentación que no tocan el
// estado del panel: agrupar conversaciones por fecha o por carpeta, formatear
// fechas y tamaños, y pintar la cronología de acciones, el markdown y las
// tarjetas de adjunto.
//
// El panel se quedó con sus 52 hooks intactos: esta extracción no mueve lógica,
// sólo deja de mezclar lo que se puede leer y probar por separado con lo que
// depende del ciclo de vida del componente.

import {
  Brain,
  CheckCircle2,
  CircleAlert,
  Database,
  ExternalLink,
  FileImage,
  FileText,
  Globe2,
  Hammer,
  Loader2,
  MapPin,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useState } from "react";
import {
  formatAssistantTool,
  type AssistantAction,
  type AssistantConversation,
  type AssistantConversationFolder,
  type AssistantMessageAttachment,
} from "../types";

export const UNCATEGORIZED_FOLDER_ID = "sin-carpeta";
export function normalizeFolderName(value: string) {
  return value.trim().replace(/\s+/g, " ");
}

type ParsedActionResult = {
  data: unknown | null;
  text: string;
};

export type ConversationFolderId = number | typeof UNCATEGORIZED_FOLDER_ID;

export type ConversationGroup = {
  id: ConversationFolderId | string;
  label: string;
  conversations: AssistantConversation[];
};

export function stableMessageIndex(seed: number | string, length: number) {
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

export function AssistantSymbolIcon({
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

export function buildRecentConversationGroups(
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

export function buildFolderConversationGroups(
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

export function formatDate(value: string) {
  return new Intl.DateTimeFormat("es-ES", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

export function formatShortDate(value: string) {
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
  if (tool === "web_search" || tool === "read_web_page") {
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
  if (
    !["web_search", "read_web_page"].includes(action.tool) ||
    !action.ok
  ) {
    return [];
  }

  const parsed = parseActionResult(action.result);
  if (!parsed.data || typeof parsed.data !== "object") {
    return [];
  }

  if (action.tool === "read_web_page") {
    const page = parsed.data as Record<string, unknown>;
    if (!page.final_url || !page.source_url) {
      return [];
    }
    const textChars = Number(page.text_char_count ?? 0);
    const sourceUrl = String(page.source_url);
    return [
      {
        title: String(page.title ?? page.final_url),
        url: String(page.final_url),
        snippet: Number.isFinite(textChars)
          ? `${textChars.toLocaleString("es-ES")} caracteres extraídos. Origen localizado: ${sourceUrl}`
          : `Fuente leída. Origen localizado: ${sourceUrl}`,
        publishedAt: "",
      },
    ];
  }

  if (
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

export function AssistantMapActions({ actions }: { actions: AssistantAction[] }) {
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

export function ActionTimeline({
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

export function AssistantMarkdown({ content }: { content: string }) {
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

const ATTACHMENT_STATUS_LABELS: Record<
  AssistantMessageAttachment["context_status"],
  string
> = {
  pending: "Preparando",
  ready: "Texto usado solo en este turno",
  empty: "Sin texto extraíble",
  unsupported: "Formato adjunto, pero no analizado",
  vision_unavailable: "Imagen adjunta · visión no disponible",
  too_large: "Demasiado grande para lectura automática",
  unavailable: "Archivo no disponible",
  failed: "No se pudo leer",
};

export function formatAttachmentSize(sizeBytes: number) {
  if (sizeBytes < 1024) {
    return `${sizeBytes} B`;
  }
  if (sizeBytes < 1024 * 1024) {
    return `${Math.ceil(sizeBytes / 1024)} KB`;
  }
  return `${(sizeBytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function MessageAttachmentCard({
  attachment,
  onLoadPreview,
  onOpen,
}: {
  attachment: AssistantMessageAttachment;
  onLoadPreview: (documentId: number) => Promise<string>;
  onOpen: (documentId: number) => Promise<void>;
}) {
  const isImage = attachment.content_type.startsWith("image/");
  const [previewUrl, setPreviewUrl] = useState("");
  const [previewState, setPreviewState] = useState<
    "idle" | "loading" | "error"
  >("idle");

  async function handleLoadPreview() {
    if (!isImage || previewState === "loading") {
      return;
    }
    setPreviewState("loading");
    try {
      const url = await onLoadPreview(attachment.document_id);
      if (url) {
        setPreviewUrl(url);
      }
      setPreviewState("idle");
    } catch {
      setPreviewState("error");
    }
  }

  return (
    <div className="assistant-message-attachment">
      <div className="assistant-message-attachment-preview">
        {previewUrl ? (
          // The URL is an authenticated local blob, never a remote source.
          // eslint-disable-next-line @next/next/no-img-element
          <img alt={`Vista previa de ${attachment.filename}`} src={previewUrl} />
        ) : isImage ? (
          <button
            aria-label={`Cargar vista previa de ${attachment.filename}`}
            disabled={previewState === "loading"}
            onClick={() => void handleLoadPreview()}
            title={
              previewState === "error"
                ? "Reintentar vista previa"
                : "Cargar vista previa"
            }
            type="button"
          >
            {previewState === "loading" ? (
              <Loader2 aria-hidden className="spinning-icon" size={18} />
            ) : previewState === "error" ? (
              <CircleAlert aria-hidden size={18} />
            ) : (
              <FileImage aria-hidden size={20} />
            )}
          </button>
        ) : (
          <FileText aria-hidden size={20} />
        )}
      </div>
      <div className="assistant-message-attachment-copy">
        <strong title={attachment.filename}>{attachment.filename}</strong>
        <small>
          {formatAttachmentSize(attachment.size_bytes)} ·{" "}
          {ATTACHMENT_STATUS_LABELS[attachment.context_status]}
        </small>
      </div>
      <button
        aria-label={`Abrir ${attachment.filename}`}
        onClick={() => void onOpen(attachment.document_id)}
        title="Abrir archivo"
        type="button"
      >
        <ExternalLink aria-hidden size={15} />
      </button>
    </div>
  );
}
