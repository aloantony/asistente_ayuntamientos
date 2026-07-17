"use client";

import {
  Check,
  Clipboard,
  Download,
  Eye,
  FileClock,
  FilePenLine,
  History,
  Loader2,
  Plus,
  RotateCcw,
  Save,
  X,
} from "lucide-react";
import {
  type FormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { AssistantCanvasController } from "../lib/useAssistantCanvasController";
import type {
  AssistantCanvasDocumentType,
  AssistantCanvasRevision,
  User,
} from "./types";
import styles from "./AssistantDocumentCanvas.module.css";

type CanvasTab = "edit" | "preview" | "history";
const CANVAS_TABS: CanvasTab[] = ["edit", "preview", "history"];

const DOCUMENT_TYPES: Array<{
  value: AssistantCanvasDocumentType;
  label: string;
}> = [
  { value: "municipal_ordinance", label: "Ordenanza municipal" },
  { value: "regulation", label: "Reglamento" },
  { value: "report", label: "Informe" },
  { value: "letter", label: "Carta" },
  { value: "minutes", label: "Acta" },
  { value: "other", label: "Otro documento" },
];

const REVISION_SOURCE_LABELS: Record<
  AssistantCanvasRevision["edit_source"],
  string
> = {
  user: "Tú",
  assistant: "Anacleto",
  restore: "Restauración",
};

const CANVAS_MARKDOWN_ELEMENTS = [
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
];

function formatDate(value: string) {
  return new Intl.DateTimeFormat("es-ES", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function safeFilename(title: string) {
  const normalized = title
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
  return `${normalized || "borrador"}.md`;
}

type AssistantDocumentCanvasProps = {
  canvas: AssistantCanvasController;
  currentUser: User;
};

export function AssistantDocumentCanvas({
  canvas,
  currentUser,
}: AssistantDocumentCanvasProps) {
  const [tab, setTab] = useState<CanvasTab>("edit");
  const [showCreate, setShowCreate] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [newType, setNewType] =
    useState<AssistantCanvasDocumentType>("municipal_ordinance");
  const [newOrganizationId, setNewOrganizationId] = useState<number | null>(
    currentUser.organizations?.length === 1
      ? currentUser.organizations[0].id
      : null,
  );
  const [copyStatus, setCopyStatus] = useState<"idle" | "copied">("idle");
  const [historyLoadedFor, setHistoryLoadedFor] = useState<number | null>(null);
  const canvasRef = useRef<HTMLElement | null>(null);
  const canvasDocument = canvas.document;
  const canvasDocumentId = canvasDocument?.id ?? null;
  const canvasDocumentRevision = canvasDocument?.current_revision ?? null;
  const canvasDocumentCount = canvas.workspace.documents.length;
  const canvasRevisionCount = canvas.revisions.length;
  const canvasRevisionsLoading = canvas.isLoadingRevisions;
  const loadCanvasRevisions = canvas.loadRevisions;
  const saveCanvasDocument = canvas.saveDocument;
  const canvasIsOpen = canvas.isOpen;

  const wordCount = useMemo(() => {
    const words = canvas.draftContent.trim().match(/\S+/g);
    return words?.length ?? 0;
  }, [canvas.draftContent]);

  useEffect(() => {
    setShowCreate(canvasDocumentId === null && canvasDocumentCount === 0);
    setTab("edit");
    setHistoryLoadedFor(null);
  }, [canvasDocumentCount, canvasDocumentId]);

  useEffect(() => {
    setHistoryLoadedFor(null);
  }, [canvasDocumentId, canvasDocumentRevision]);

  useEffect(() => {
    if (
      tab === "history" &&
      canvasDocumentId !== null &&
      canvasRevisionCount === 0 &&
      !canvasRevisionsLoading &&
      historyLoadedFor !== canvasDocumentId
    ) {
      setHistoryLoadedFor(canvasDocumentId);
      void loadCanvasRevisions();
    }
  }, [
    canvasDocumentId,
    canvasRevisionCount,
    canvasRevisionsLoading,
    historyLoadedFor,
    loadCanvasRevisions,
    tab,
  ]);

  useEffect(() => {
    const saveShortcut = (event: KeyboardEvent) => {
      if (
        canvasIsOpen &&
        (event.metaKey || event.ctrlKey) &&
        event.key.toLowerCase() === "s"
      ) {
        event.preventDefault();
        void saveCanvasDocument();
      }
    };
    window.addEventListener("keydown", saveShortcut);
    return () => window.removeEventListener("keydown", saveShortcut);
  }, [canvasIsOpen, saveCanvasDocument]);

  useEffect(() => {
    canvasRef.current?.focus({ preventScroll: true });
  }, []);

  useEffect(() => {
    setNewTitle("");
    setNewType("municipal_ordinance");
    setNewOrganizationId(
      currentUser.organizations?.length === 1
        ? currentUser.organizations[0].id
        : null,
    );
  }, [canvas.conversationId, currentUser.organizations]);

  async function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (canvas.assistantBusy || canvas.isLoading || canvas.isSaving) {
      return;
    }
    const created = await canvas.createDocument({
      title: newTitle.trim(),
      document_type: newType,
      organization_id: newOrganizationId,
    });
    if (created) {
      setNewTitle("");
      setShowCreate(false);
    }
  }

  async function toggleCreateForm() {
    if (showCreate) {
      setShowCreate(false);
      return;
    }
    if ((canvas.isDirty || canvas.conflict) && !(await canvas.saveDocument())) {
      return;
    }
    setShowCreate(true);
  }

  function handleTabKeyDown(
    event: ReactKeyboardEvent<HTMLButtonElement>,
    currentTab: CanvasTab,
  ) {
    const currentIndex = CANVAS_TABS.indexOf(currentTab);
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") {
      nextIndex = (currentIndex + 1) % CANVAS_TABS.length;
    } else if (event.key === "ArrowLeft") {
      nextIndex = (currentIndex - 1 + CANVAS_TABS.length) % CANVAS_TABS.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = CANVAS_TABS.length - 1;
    }
    if (nextIndex === null) {
      return;
    }
    event.preventDefault();
    const nextTab = CANVAS_TABS[nextIndex];
    setTab(nextTab);
    window.requestAnimationFrame(() => {
      window.document.getElementById(`canvas-tab-${nextTab}`)?.focus();
    });
  }

  async function copyDocument() {
    try {
      await navigator.clipboard.writeText(canvas.draftContent);
      setCopyStatus("copied");
      window.setTimeout(() => setCopyStatus("idle"), 1600);
    } catch {
      // Clipboard access can be denied by the browser. Download remains
      // available as a permission-free fallback.
    }
  }

  function downloadDocument() {
    if (!canvas.document) {
      return;
    }
    const blob = new Blob([canvas.draftContent], {
      type: "text/markdown;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const link = window.document.createElement("a");
    link.href = url;
    link.download = safeFilename(canvas.draftTitle || canvas.document.title);
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <aside
      className={styles.canvas}
      aria-label="Lienzo documental"
      ref={canvasRef}
      tabIndex={-1}
    >
      <header className={styles.header}>
        <div className={styles.identity}>
          <span className={styles.draftBadge}>Borrador · No oficial</span>
          <span className={styles.revision}>
            {canvas.document ? `Revisión ${canvas.document.current_revision}` : "Lienzo"}
          </span>
        </div>
        <button
          className={styles.iconButton}
          type="button"
          onClick={() => void canvas.closeCanvas()}
          disabled={
            canvas.assistantBusy || canvas.isLoading || canvas.isSaving
          }
          aria-label="Cerrar lienzo"
          title="Cerrar lienzo"
        >
          <X size={18} aria-hidden="true" />
        </button>
      </header>

      <div className={styles.documentBar}>
        <label className={styles.srOnly} htmlFor="canvas-document-selector">
          Borrador abierto
        </label>
        <select
          id="canvas-document-selector"
          className={styles.documentSelector}
          value={canvas.document?.id ?? ""}
          onChange={(event) => {
            const documentId = Number.parseInt(event.target.value, 10);
            if (Number.isInteger(documentId)) {
              void canvas.openDocument(documentId);
            }
          }}
          disabled={
            canvas.isLoading ||
            canvas.isSaving ||
            canvas.assistantBusy ||
            canvas.workspace.documents.length === 0
          }
        >
          <option value="">Selecciona un borrador</option>
          {canvas.workspace.documents.map((candidate) => (
            <option key={candidate.id} value={candidate.id}>
              {candidate.title} · r{candidate.current_revision}
            </option>
          ))}
        </select>
        <button
          className={styles.compactButton}
          type="button"
          onClick={() => void toggleCreateForm()}
          disabled={
            canvas.assistantBusy ||
            canvas.isLoading ||
            canvas.isSaving ||
            !canvas.canCreate
          }
        >
          <Plus size={16} aria-hidden="true" />
          Nuevo
        </button>
      </div>

      {canvas.error && !canvas.document ? (
        <div className={styles.errorBanner} role="alert">
          <p>{canvas.error}</p>
        </div>
      ) : null}

      {canvas.isLoading && !canvas.document ? (
        <p className={styles.emptyState}>
          <Loader2 className={styles.spin} size={20} aria-hidden="true" />
          Cargando lienzo…
        </p>
      ) : showCreate ? (
        <form className={styles.createForm} onSubmit={handleCreate}>
          <div>
            <p className={styles.eyebrow}>Nuevo documento de trabajo</p>
            <h2>Crear borrador</h2>
            <p>
              Quedará ligado a esta conversación, con historial de revisiones y
              sin valor oficial.
            </p>
          </div>
          <label>
            Título
            <input
              value={newTitle}
              onChange={(event) => setNewTitle(event.target.value)}
              maxLength={255}
              placeholder="Ordenanza municipal de…"
              required
              autoFocus
              disabled={
                !canvas.canCreate ||
                canvas.assistantBusy ||
                canvas.isLoading ||
                canvas.isSaving
              }
            />
          </label>
          <label>
            Tipo de documento
            <select
              value={newType}
              disabled={
                !canvas.canCreate ||
                canvas.assistantBusy ||
                canvas.isLoading ||
                canvas.isSaving
              }
              onChange={(event) =>
                setNewType(event.target.value as AssistantCanvasDocumentType)
              }
            >
              {DOCUMENT_TYPES.map((documentType) => (
                <option key={documentType.value} value={documentType.value}>
                  {documentType.label}
                </option>
              ))}
            </select>
          </label>
          {(currentUser.organizations?.length ?? 0) > 0 ? (
            <label>
              Ayuntamiento
              <select
                value={newOrganizationId ?? ""}
                disabled={
                  !canvas.canCreate ||
                  canvas.assistantBusy ||
                  canvas.isLoading ||
                  canvas.isSaving
                }
                onChange={(event) =>
                  setNewOrganizationId(
                    event.target.value
                      ? Number.parseInt(event.target.value, 10)
                      : null,
                  )
                }
              >
                <option value="">Sin vincular</option>
                {currentUser.organizations?.map((organization) => (
                  <option key={organization.id} value={organization.id}>
                    {organization.name}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          <div className={styles.formActions}>
            {canvas.workspace.documents.length > 0 ? (
              <button
                className={styles.secondaryButton}
                type="button"
                onClick={() => setShowCreate(false)}
                disabled={
                  canvas.assistantBusy || canvas.isLoading || canvas.isSaving
                }
              >
                Cancelar
              </button>
            ) : null}
            <button
              className={styles.primaryButton}
              type="submit"
              disabled={
                !newTitle.trim() ||
                canvas.assistantBusy ||
                canvas.isLoading ||
                canvas.isSaving ||
                !canvas.canCreate
              }
            >
              {canvas.isLoading ? (
                <Loader2 className={styles.spin} size={16} aria-hidden="true" />
              ) : (
                <FilePenLine size={16} aria-hidden="true" />
              )}
              Crear borrador
            </button>
          </div>
        </form>
      ) : canvas.document ? (
        <>
          <div className={styles.titleArea}>
            <label className={styles.srOnly} htmlFor="canvas-document-title">
              Título del borrador
            </label>
            <input
              id="canvas-document-title"
              className={styles.titleInput}
              value={canvas.draftTitle}
              onChange={(event) => canvas.setDraftTitle(event.target.value)}
              maxLength={255}
              readOnly={
                !canvas.canEdit ||
                canvas.assistantBusy ||
                canvas.isLoading ||
                canvas.isSaving
              }
            />
            <div className={styles.saveState} aria-live="polite">
              {canvas.isSaving ? (
                <>
                  <Loader2 className={styles.spin} size={14} aria-hidden="true" />
                  Guardando…
                </>
              ) : canvas.isDirty ? (
                "Cambios sin guardar"
              ) : (
                <>
                  <Check size={14} aria-hidden="true" />
                  Guardado
                </>
              )}
            </div>
          </div>

          {canvas.error ? (
            <div className={styles.errorBanner} role="alert">
              <p>{canvas.error}</p>
              {canvas.conflict ? (
                <div className={styles.bannerActions}>
                  <button
                    type="button"
                    onClick={canvas.useServerConflict}
                    className={styles.secondaryButton}
                    disabled={
                      canvas.assistantBusy || canvas.isLoading || canvas.isSaving
                    }
                  >
                    Cargar revisión {canvas.conflict.current_revision}
                  </button>
                  <button
                    type="button"
                    onClick={() => void canvas.saveOverConflict()}
                    className={styles.primaryButton}
                    disabled={
                      canvas.assistantBusy ||
                      canvas.isLoading ||
                      canvas.isSaving ||
                      !canvas.draftTitle.trim()
                    }
                  >
                    Mantener mi texto
                  </button>
                </div>
              ) : null}
            </div>
          ) : null}

          <nav
            className={styles.tabs}
            aria-label="Vistas del borrador"
            role="tablist"
          >
            <button
              aria-controls="canvas-document-panel"
              className={tab === "edit" ? styles.activeTab : styles.tab}
              id="canvas-tab-edit"
              role="tab"
              type="button"
              onClick={() => setTab("edit")}
              onKeyDown={(event) => handleTabKeyDown(event, "edit")}
              aria-selected={tab === "edit"}
              tabIndex={tab === "edit" ? 0 : -1}
            >
              <FilePenLine size={16} aria-hidden="true" />
              Editar
            </button>
            <button
              aria-controls="canvas-document-panel"
              className={tab === "preview" ? styles.activeTab : styles.tab}
              id="canvas-tab-preview"
              role="tab"
              type="button"
              onClick={() => setTab("preview")}
              onKeyDown={(event) => handleTabKeyDown(event, "preview")}
              aria-selected={tab === "preview"}
              tabIndex={tab === "preview" ? 0 : -1}
            >
              <Eye size={16} aria-hidden="true" />
              Vista previa
            </button>
            <button
              aria-controls="canvas-document-panel"
              className={tab === "history" ? styles.activeTab : styles.tab}
              id="canvas-tab-history"
              role="tab"
              type="button"
              onClick={() => setTab("history")}
              onKeyDown={(event) => handleTabKeyDown(event, "history")}
              aria-selected={tab === "history"}
              tabIndex={tab === "history" ? 0 : -1}
            >
              <History size={16} aria-hidden="true" />
              Historial
            </button>
          </nav>

          <div
            aria-labelledby={`canvas-tab-${tab}`}
            className={styles.contentArea}
            id="canvas-document-panel"
            role="tabpanel"
          >
            {tab === "edit" ? (
              <textarea
                className={styles.editor}
                value={canvas.draftContent}
                onChange={(event) => canvas.setDraftContent(event.target.value)}
                maxLength={60_000}
                readOnly={
                  !canvas.canEdit ||
                  canvas.assistantBusy ||
                  canvas.isLoading ||
                  canvas.isSaving
                }
                aria-label="Contenido del borrador en Markdown"
                spellCheck
              />
            ) : null}
            {tab === "preview" ? (
              <article className={styles.preview}>
                <ReactMarkdown
                  allowedElements={CANVAS_MARKDOWN_ELEMENTS}
                  components={{
                    a: ({ href, children }) => (
                      <a
                        href={href}
                        rel="noreferrer noopener"
                        target="_blank"
                      >
                        {children}
                      </a>
                    ),
                  }}
                  remarkPlugins={[remarkGfm]}
                >
                  {canvas.draftContent || "_El borrador todavía está vacío._"}
                </ReactMarkdown>
              </article>
            ) : null}
            {tab === "history" ? (
              <section className={styles.history} aria-label="Historial de revisiones">
                {canvas.isLoadingRevisions ? (
                  <p className={styles.emptyState}>
                    <Loader2 className={styles.spin} size={18} aria-hidden="true" />
                    Cargando historial…
                  </p>
                ) : canvas.revisions.length > 0 ? (
                  <ol>
                    {canvas.revisions.map((revision) => (
                      <li key={revision.id}>
                        <div className={styles.revisionHeader}>
                          <div>
                            <strong>Revisión {revision.revision_number}</strong>
                            <span>{REVISION_SOURCE_LABELS[revision.edit_source]}</span>
                          </div>
                          <time dateTime={revision.created_at}>
                            {formatDate(revision.created_at)}
                          </time>
                        </div>
                        <p>
                          {revision.change_summary || "Revisión guardada"}
                        </p>
                        {revision.content_excerpt ? (
                          <blockquote>{revision.content_excerpt}</blockquote>
                        ) : null}
                        {revision.revision_number !==
                        canvas.document?.current_revision ? (
                          <button
                            className={styles.restoreButton}
                            type="button"
                            disabled={
                              !canvas.canEdit ||
                              canvas.isDirty ||
                              canvas.isLoading ||
                              canvas.isSaving ||
                              canvas.assistantBusy
                            }
                            onClick={() => {
                              if (
                                window.confirm(
                                  `¿Restaurar la revisión ${revision.revision_number}? Se creará una revisión nueva.`,
                                )
                              ) {
                                void canvas.restoreRevision(
                                  revision.revision_number,
                                );
                              }
                            }}
                          >
                            <RotateCcw size={15} aria-hidden="true" />
                            Restaurar
                          </button>
                        ) : null}
                      </li>
                    ))}
                  </ol>
                ) : (
                  <div className={styles.emptyState}>
                    <span>No hay revisiones disponibles.</span>
                    <button
                      className={styles.secondaryButton}
                      type="button"
                      onClick={() => void canvas.loadRevisions()}
                      disabled={
                        canvas.assistantBusy ||
                        canvas.isLoading ||
                        canvas.isLoadingRevisions ||
                        canvas.isSaving
                      }
                    >
                      Reintentar
                    </button>
                  </div>
                )}
              </section>
            ) : null}
          </div>

          <footer className={styles.footer}>
            <div className={styles.documentStats}>
              <span>{wordCount.toLocaleString("es-ES")} palabras</span>
              <span>{canvas.draftContent.length.toLocaleString("es-ES")} / 60.000</span>
            </div>
            <div className={styles.footerActions}>
              <button
                className={styles.iconButton}
                type="button"
                onClick={() => void copyDocument()}
                title="Copiar Markdown"
                aria-label="Copiar contenido"
              >
                {copyStatus === "copied" ? (
                  <Check size={17} aria-hidden="true" />
                ) : (
                  <Clipboard size={17} aria-hidden="true" />
                )}
              </button>
              <button
                className={styles.iconButton}
                type="button"
                onClick={downloadDocument}
                title="Descargar Markdown"
                aria-label="Descargar borrador en Markdown"
              >
                <Download size={17} aria-hidden="true" />
              </button>
              <button
                className={styles.primaryButton}
                type="button"
                onClick={() => void canvas.saveDocument()}
                disabled={
                  !canvas.canEdit ||
                  !canvas.isDirty ||
                  Boolean(canvas.conflict) ||
                  canvas.isLoading ||
                  canvas.isSaving ||
                  canvas.assistantBusy ||
                  !canvas.draftTitle.trim()
                }
              >
                {canvas.isSaving ? (
                  <Loader2 className={styles.spin} size={16} aria-hidden="true" />
                ) : (
                  <Save size={16} aria-hidden="true" />
                )}
                Guardar
              </button>
            </div>
          </footer>
        </>
      ) : (
        <div className={styles.emptyCanvas}>
          <FileClock size={34} aria-hidden="true" />
          <h2>Un espacio para desarrollar documentos</h2>
          <p>
            Crea aquí una ordenanza, informe, acta u otro borrador y trabaja en
            él junto a Anacleto.
          </p>
          <button
            className={styles.primaryButton}
            type="button"
            onClick={() => setShowCreate(true)}
            disabled={
              !canvas.canCreate ||
              canvas.assistantBusy ||
              canvas.isLoading ||
              canvas.isSaving
            }
          >
            <Plus size={16} aria-hidden="true" />
            Crear primer borrador
          </button>
        </div>
      )}
    </aside>
  );
}
