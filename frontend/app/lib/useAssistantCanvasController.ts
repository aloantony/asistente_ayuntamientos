"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  AssistantCanvasDocument,
  AssistantCanvasDocumentSummary,
  AssistantCanvasDocumentType,
  AssistantCanvasRevision,
  AssistantCanvasWorkspace,
  AssistantConversationDetail,
  AssistantOpenCanvasUiAction,
} from "../components/types";
import {
  getMessageCanvasActions,
  parseAssistantCanvasAction,
} from "./assistantCanvasActions";
import { adminRequest, ApiRequestError } from "./api";
import { registerNavigationGuard } from "./navigationGuards";

type RequestErrorHandler = (
  requestError: unknown,
  setMessage: (message: string) => void,
  fallback: string,
) => void;

type UseAssistantCanvasControllerArgs = {
  conversation: AssistantConversationDetail | null;
  getStoredToken: () => string;
  handleRequestError: RequestErrorHandler;
  assistantBusy: boolean;
};

type CreateCanvasDocumentInput = {
  title: string;
  document_type: AssistantCanvasDocumentType;
  organization_id: number | null;
};

const EMPTY_WORKSPACE: AssistantCanvasWorkspace = {
  documents: [],
  active_document_id: null,
};

function replaceDocumentSummary(
  documents: AssistantCanvasDocumentSummary[],
  document: AssistantCanvasDocument,
) {
  const summary: AssistantCanvasDocumentSummary = {
    id: document.id,
    conversation_id: document.conversation_id,
    organization_id: document.organization_id,
    document_type: document.document_type,
    title: document.title,
    status: document.status,
    current_revision: document.current_revision,
    created_at: document.created_at,
    updated_at: document.updated_at,
  };
  return [
    summary,
    ...documents.filter((candidate) => candidate.id !== document.id),
  ];
}

export function useAssistantCanvasController({
  conversation,
  getStoredToken,
  handleRequestError,
  assistantBusy,
}: UseAssistantCanvasControllerArgs) {
  const conversationId = conversation?.id ?? null;
  const [workspace, setWorkspace] =
    useState<AssistantCanvasWorkspace>(EMPTY_WORKSPACE);
  const [document, setDocument] = useState<AssistantCanvasDocument | null>(null);
  const [draftTitle, setDraftTitle] = useState("");
  const [draftContent, setDraftContent] = useState("");
  const [revisions, setRevisions] = useState<AssistantCanvasRevision[]>([]);
  const [isOpen, setIsOpen] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [isLoadingRevisions, setIsLoadingRevisions] = useState(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState<AssistantCanvasDocument | null>(null);
  const [lastSavedAt, setLastSavedAt] = useState<string | null>(null);
  const requestGenerationRef = useRef(0);
  const documentLoadGenerationRef = useRef(0);
  const revisionLoadGenerationRef = useRef(0);
  const generationConversationIdRef = useRef(conversationId);
  if (generationConversationIdRef.current !== conversationId) {
    generationConversationIdRef.current = conversationId;
    requestGenerationRef.current += 1;
    documentLoadGenerationRef.current += 1;
  }
  const currentConversationIdRef = useRef(conversationId);
  currentConversationIdRef.current = conversationId;
  const currentDocumentIdRef = useRef<number | null>(null);
  const seenActionIdsRef = useRef(new Set<string>());
  const documentNavigationQueueRef = useRef<Promise<void>>(Promise.resolve());
  const mutationPromiseRef = useRef<Promise<boolean> | null>(null);
  const createPromiseRef = useRef<Promise<AssistantCanvasDocument | null> | null>(
    null,
  );
  const requestIdByOperationRef = useRef(new Map<string, string>());

  const isDirty = Boolean(
    document &&
      (draftTitle !== document.title || draftContent !== document.content),
  );
  const canCreate = conversation?.status === "active";
  const canEdit = conversation?.status === "active" && document?.status === "draft";
  currentDocumentIdRef.current = document?.id ?? null;

  const canvasActions = useMemo(
    () =>
      conversation?.messages.flatMap((message) =>
        getMessageCanvasActions(message.actions),
      ) ?? [],
    [conversation?.messages],
  );
  const actionSignature = canvasActions.map((action) => action.id).join("|");

  const requestIsCurrent = useCallback(
    (generation: number, targetConversationId: number | null) =>
      generation === requestGenerationRef.current &&
      targetConversationId === currentConversationIdRef.current,
    [],
  );

  const documentLoadIsCurrent = useCallback(
    (
      generation: number,
      loadGeneration: number,
      targetConversationId: number | null,
    ) =>
      requestIsCurrent(generation, targetConversationId) &&
      loadGeneration === documentLoadGenerationRef.current,
    [requestIsCurrent],
  );

  const enqueueDocumentNavigation = useCallback(
    (operation: () => Promise<boolean>) => {
      const result = documentNavigationQueueRef.current.then(operation);
      documentNavigationQueueRef.current = result.then(
        () => undefined,
        () => undefined,
      );
      return result;
    },
    [],
  );

  const requestIdFor = useCallback((operationKey: string) => {
    const existing = requestIdByOperationRef.current.get(operationKey);
    if (existing) {
      return existing;
    }
    const requestId = crypto.randomUUID();
    requestIdByOperationRef.current.set(operationKey, requestId);
    return requestId;
  }, []);

  const forgetRequestId = useCallback((operationKey: string) => {
    requestIdByOperationRef.current.delete(operationKey);
  }, []);

  const applyDocument = useCallback((nextDocument: AssistantCanvasDocument) => {
    if (nextDocument.conversation_id !== currentConversationIdRef.current) {
      return false;
    }
    setDocument(nextDocument);
    setDraftTitle(nextDocument.title);
    setDraftContent(nextDocument.content);
    setConflict(null);
    revisionLoadGenerationRef.current += 1;
    setRevisions([]);
    setIsLoadingRevisions(false);
    setLastSavedAt(nextDocument.updated_at);
    setWorkspace((current) => ({
      documents: replaceDocumentSummary(current.documents, nextDocument),
      active_document_id: nextDocument.id,
    }));
    return true;
  }, []);

  const applyConflict = useCallback((nextDocument: AssistantCanvasDocument) => {
    if (nextDocument.conversation_id !== currentConversationIdRef.current) {
      return false;
    }
    setDocument(nextDocument);
    setConflict(nextDocument);
    revisionLoadGenerationRef.current += 1;
    setRevisions([]);
    setIsLoadingRevisions(false);
    setLastSavedAt(nextDocument.updated_at);
    setWorkspace((current) => ({
      documents: replaceDocumentSummary(current.documents, nextDocument),
      active_document_id: nextDocument.id,
    }));
    return true;
  }, []);

  const fetchDocument = useCallback(
    async (
      documentId: number,
      generation = requestGenerationRef.current,
      targetConversationId = currentConversationIdRef.current,
      loadGeneration = documentLoadGenerationRef.current,
    ) => {
      const nextDocument = await adminRequest<AssistantCanvasDocument>(
        `/assistant/canvas/documents/${documentId}`,
        getStoredToken(),
        "No se pudo cargar el borrador.",
      );
      if (
        documentLoadIsCurrent(
          generation,
          loadGeneration,
          targetConversationId,
        ) &&
        nextDocument.conversation_id === targetConversationId
      ) {
        applyDocument(nextDocument);
      }
      return nextDocument;
    },
    [applyDocument, documentLoadIsCurrent, getStoredToken],
  );

  const setActiveDocument = useCallback(
    async (documentId: number | null, targetConversationId = conversationId) => {
      if (!targetConversationId) {
        return;
      }
      await adminRequest<{ active_document_id: number | null }>(
        `/assistant/conversations/${targetConversationId}/canvas/active`,
        getStoredToken(),
        "No se pudo cambiar el borrador activo.",
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ document_id: documentId }),
        },
      );
    },
    [conversationId, getStoredToken],
  );

  const reportError = useCallback(
    (requestError: unknown, fallback: string) => {
      handleRequestError(requestError, setError, fallback);
    },
    [handleRequestError],
  );

  useEffect(() => {
    const generation = requestGenerationRef.current;
    const loadGeneration = ++documentLoadGenerationRef.current;
    revisionLoadGenerationRef.current += 1;
    const targetConversationId = conversationId;
    const historicalActionIds = new Set(
      canvasActions.map((action) => action.id),
    );
    seenActionIdsRef.current = historicalActionIds;
    setWorkspace(EMPTY_WORKSPACE);
    setDocument(null);
    setDraftTitle("");
    setDraftContent("");
    setRevisions([]);
    setConflict(null);
    setError("");
    setLastSavedAt(null);
    setIsOpen(false);
    setIsLoading(false);
    setIsSaving(false);
    setIsLoadingRevisions(false);
    mutationPromiseRef.current = null;
    createPromiseRef.current = null;
    requestIdByOperationRef.current.clear();

    if (!targetConversationId) {
      return;
    }

    setIsLoading(true);
    adminRequest<AssistantCanvasWorkspace>(
      `/assistant/conversations/${targetConversationId}/canvas`,
      getStoredToken(),
      "No se pudo cargar el lienzo.",
    )
      .then(async (nextWorkspace) => {
        if (!requestIsCurrent(generation, targetConversationId)) {
          return;
        }
        setWorkspace(nextWorkspace);
        if (nextWorkspace.active_document_id) {
          setIsOpen(true);
          await fetchDocument(
            nextWorkspace.active_document_id,
            generation,
            targetConversationId,
            loadGeneration,
          );
        }
      })
      .catch((requestError: unknown) => {
        if (requestIsCurrent(generation, targetConversationId)) {
          reportError(requestError, "No se pudo cargar el lienzo.");
        }
      })
      .finally(() => {
        if (requestIsCurrent(generation, targetConversationId)) {
          setIsLoading(false);
        }
      });
  }, [conversationId]); // eslint-disable-line react-hooks/exhaustive-deps

  const openFromAction = useCallback(
    async (
      action: AssistantOpenCanvasUiAction,
      options: { allowDirtyDiscard?: boolean } = {},
    ) => {
      if (
        action.context.conversation_id !== currentConversationIdRef.current ||
        seenActionIdsRef.current.has(action.id)
      ) {
        return false;
      }
      if (assistantBusy || isLoading || isSaving) {
        return false;
      }
      seenActionIdsRef.current.add(action.id);
      setIsOpen(true);
      setError("");
      if (
        isDirty &&
        document?.id !== action.context.document_id &&
        !options.allowDirtyDiscard
      ) {
        setError(
          "Anacleto ha actualizado otro borrador. Guarda o descarta tus cambios actuales antes de abrirlo desde el mensaje.",
        );
        return false;
      }
      const generation = requestGenerationRef.current;
      const loadGeneration = ++documentLoadGenerationRef.current;
      const targetConversationId = action.context.conversation_id;
      setIsLoading(true);
      try {
        await setActiveDocument(
          action.context.document_id,
          targetConversationId,
        );
        const nextDocument = await adminRequest<AssistantCanvasDocument>(
          `/assistant/canvas/documents/${action.context.document_id}`,
          getStoredToken(),
          "No se pudo abrir el borrador actualizado.",
        );
        if (
          !documentLoadIsCurrent(
            generation,
            loadGeneration,
            targetConversationId,
          ) ||
          nextDocument.conversation_id !== targetConversationId
        ) {
          return false;
        }
        if (isDirty && document?.id === nextDocument.id) {
          applyConflict(nextDocument);
          setError(
            "El asistente ha creado una revisión mientras tenías cambios locales. Revisa el conflicto antes de guardar.",
          );
          return false;
        }
        applyDocument(nextDocument);
        return true;
      } catch (requestError) {
        if (
          documentLoadIsCurrent(
            generation,
            loadGeneration,
            targetConversationId,
          )
        ) {
          reportError(requestError, "No se pudo abrir el borrador actualizado.");
        }
        return false;
      } finally {
        if (
          documentLoadIsCurrent(
            generation,
            loadGeneration,
            targetConversationId,
          )
        ) {
          setIsLoading(false);
        }
      }
    },
    [
      applyConflict,
      applyDocument,
      assistantBusy,
      document?.id,
      documentLoadIsCurrent,
      getStoredToken,
      isDirty,
      isLoading,
      isSaving,
      reportError,
      setActiveDocument,
    ],
  );

  const enqueueActionOpen = useCallback(
    (
      action: AssistantOpenCanvasUiAction,
      options: { allowDirtyDiscard?: boolean } = {},
    ) => {
      return enqueueDocumentNavigation(() => openFromAction(action, options));
    },
    [enqueueDocumentNavigation, openFromAction],
  );

  useEffect(() => {
    if (assistantBusy || isLoading || isSaving) {
      return;
    }
    const unseenActions = canvasActions.filter(
      (action) => !seenActionIdsRef.current.has(action.id),
    );
    if (unseenActions.length === 0) {
      return;
    }
    for (const supersededAction of unseenActions.slice(0, -1)) {
      seenActionIdsRef.current.add(supersededAction.id);
    }
    void enqueueActionOpen(unseenActions[unseenActions.length - 1]);
  }, [
    actionSignature,
    assistantBusy,
    canvasActions,
    enqueueActionOpen,
    isLoading,
    isSaving,
  ]);

  useEffect(() => {
    if (!isDirty) {
      return;
    }
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, [isDirty]);

  const saveDocument = useCallback(() => {
    if (mutationPromiseRef.current) {
      return mutationPromiseRef.current;
    }
    if (!document || !isDirty) {
      return Promise.resolve(true);
    }
    if (conflict) {
      setError(
        "Resuelve el conflicto antes de guardar: carga la revisión del servidor o confirma que quieres mantener tu texto.",
      );
      return Promise.resolve(false);
    }
    if (!canEdit) {
      setError("Este borrador no se puede editar en su estado actual.");
      return Promise.resolve(false);
    }
    const normalizedTitle = draftTitle.trim();
    if (!normalizedTitle) {
      setError("El borrador necesita un título antes de guardarse.");
      return Promise.resolve(false);
    }

    const generation = requestGenerationRef.current;
    const targetConversationId = conversationId;
    const targetDocumentId = document.id;
    const operationKey = JSON.stringify({
      kind: "save",
      documentId: targetDocumentId,
      expectedRevision: document.current_revision,
      title: normalizedTitle,
      content: draftContent,
    });
    const mutationId = requestIdFor(operationKey);
    setIsSaving(true);
    setError("");
    const operation = (async () => {
      try {
        const saved = await adminRequest<AssistantCanvasDocument>(
          `/assistant/canvas/documents/${targetDocumentId}`,
          getStoredToken(),
          "No se pudo guardar el borrador.",
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              expected_revision: document.current_revision,
              title: normalizedTitle,
              content: draftContent,
              change_summary: "Edición manual en el lienzo",
              mutation_id: mutationId,
            }),
          },
        );
        if (!requestIsCurrent(generation, targetConversationId)) {
          return false;
        }
        forgetRequestId(operationKey);
        return applyDocument(saved);
      } catch (requestError) {
        if (!requestIsCurrent(generation, targetConversationId)) {
          return false;
        }
        if (
          requestError instanceof ApiRequestError &&
          requestError.status === 409 &&
          requestError.message === "El borrador cambió mientras lo editabas."
        ) {
          forgetRequestId(operationKey);
          try {
            const current = await adminRequest<AssistantCanvasDocument>(
              `/assistant/canvas/documents/${targetDocumentId}`,
              getStoredToken(),
              "No se pudo recuperar la revisión actual.",
            );
            if (
              requestIsCurrent(generation, targetConversationId) &&
              currentDocumentIdRef.current === targetDocumentId
            ) {
              applyConflict(current);
              setError(
                "El borrador cambió mientras editabas. Conservamos tu texto para que elijas qué versión mantener.",
              );
            }
          } catch (reloadError) {
            if (requestIsCurrent(generation, targetConversationId)) {
              reportError(reloadError, "No se pudo recuperar la revisión actual.");
            }
          }
        } else {
          if (requestError instanceof ApiRequestError) {
            forgetRequestId(operationKey);
          }
          reportError(requestError, "No se pudo guardar el borrador.");
        }
        return false;
      } finally {
        if (requestIsCurrent(generation, targetConversationId)) {
          setIsSaving(false);
        }
      }
    })();
    mutationPromiseRef.current = operation;
    void operation.finally(() => {
      if (mutationPromiseRef.current === operation) {
        mutationPromiseRef.current = null;
      }
    });
    return operation;
  }, [
    applyConflict,
    applyDocument,
    canEdit,
    conflict,
    conversationId,
    document,
    draftContent,
    draftTitle,
    forgetRequestId,
    getStoredToken,
    isDirty,
    reportError,
    requestIdFor,
    requestIsCurrent,
  ]);

  const saveIfNeeded = useCallback(async () => {
    if (mutationPromiseRef.current) {
      return mutationPromiseRef.current;
    }
    if (createPromiseRef.current) {
      return (await createPromiseRef.current) !== null;
    }
    if (!isDirty) {
      return true;
    }
    if (conflict || !canEdit) {
      setError(
        conflict
          ? "Resuelve el conflicto del borrador antes de continuar."
          : "Hay cambios locales que no se pueden guardar en el estado actual.",
      );
      return false;
    }
    return saveDocument();
  }, [canEdit, conflict, isDirty, saveDocument]);

  useEffect(
    () => registerNavigationGuard(saveIfNeeded),
    [saveIfNeeded],
  );

  useEffect(() => {
    if (
      !isDirty ||
      !canEdit ||
      conflict ||
      assistantBusy ||
      isLoading ||
      isSaving
    ) {
      return;
    }
    const timeoutId = window.setTimeout(() => {
      void saveDocument();
    }, 1200);
    return () => window.clearTimeout(timeoutId);
  }, [
    assistantBusy,
    canEdit,
    conflict,
    draftContent,
    draftTitle,
    isDirty,
    isLoading,
    isSaving,
    saveDocument,
  ]);

  useEffect(() => {
    if (!isDirty) {
      return;
    }
    const saveBeforeInternalNavigation = (event: globalThis.MouseEvent) => {
      if (
        event.defaultPrevented ||
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey ||
        !(event.target instanceof Element)
      ) {
        return;
      }
      const anchor = event.target.closest<HTMLAnchorElement>("a[href]");
      if (
        !anchor ||
        anchor.target === "_blank" ||
        anchor.hasAttribute("download")
      ) {
        return;
      }
      const destination = new URL(anchor.href, window.location.href);
      if (
        destination.origin !== window.location.origin ||
        destination.href === window.location.href
      ) {
        return;
      }
      event.preventDefault();
      void saveIfNeeded().then((saved) => {
        if (saved) {
          window.location.assign(destination.href);
        }
      });
    };
    window.document.addEventListener("click", saveBeforeInternalNavigation, true);
    return () =>
      window.document.removeEventListener(
        "click",
        saveBeforeInternalNavigation,
        true,
      );
  }, [isDirty, saveIfNeeded]);

  const saveOverConflict = useCallback(() => {
    if (mutationPromiseRef.current) {
      return mutationPromiseRef.current;
    }
    if (!document || !conflict || !canEdit) {
      return Promise.resolve(false);
    }
    const normalizedTitle = draftTitle.trim();
    if (!normalizedTitle) {
      setError("El borrador necesita un título antes de guardarse.");
      return Promise.resolve(false);
    }
    const generation = requestGenerationRef.current;
    const targetConversationId = conversationId;
    const targetDocumentId = document.id;
    const operationKey = JSON.stringify({
      kind: "resolve-conflict",
      documentId: targetDocumentId,
      expectedRevision: conflict.current_revision,
      title: normalizedTitle,
      content: draftContent,
    });
    const mutationId = requestIdFor(operationKey);
    setIsSaving(true);
    setError("");
    const operation = (async () => {
      try {
        const saved = await adminRequest<AssistantCanvasDocument>(
          `/assistant/canvas/documents/${targetDocumentId}`,
          getStoredToken(),
          "No se pudieron aplicar tus cambios sobre la revisión actual.",
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              expected_revision: conflict.current_revision,
              title: normalizedTitle,
              content: draftContent,
              change_summary: "Resolución manual de conflicto en el lienzo",
              mutation_id: mutationId,
            }),
          },
        );
        if (!requestIsCurrent(generation, targetConversationId)) {
          return false;
        }
        forgetRequestId(operationKey);
        return applyDocument(saved);
      } catch (requestError) {
        if (!requestIsCurrent(generation, targetConversationId)) {
          return false;
        }
        if (
          requestError instanceof ApiRequestError &&
          requestError.status === 409 &&
          requestError.message === "El borrador cambió mientras lo editabas."
        ) {
          forgetRequestId(operationKey);
          try {
            const current = await adminRequest<AssistantCanvasDocument>(
              `/assistant/canvas/documents/${targetDocumentId}`,
              getStoredToken(),
              "No se pudo recuperar la revisión actual.",
            );
            if (
              requestIsCurrent(generation, targetConversationId) &&
              currentDocumentIdRef.current === targetDocumentId
            ) {
              applyConflict(current);
              setError(
                "El borrador volvió a cambiar. Conservamos tu texto y hemos cargado la revisión más reciente para que decidas de nuevo.",
              );
            }
          } catch (reloadError) {
            if (requestIsCurrent(generation, targetConversationId)) {
              reportError(
                reloadError,
                "No se pudo recuperar la revisión actual.",
              );
            }
          }
        } else {
          if (requestError instanceof ApiRequestError) {
            forgetRequestId(operationKey);
          }
          reportError(
            requestError,
            "No se pudieron aplicar tus cambios sobre la revisión actual.",
          );
        }
        return false;
      } finally {
        if (requestIsCurrent(generation, targetConversationId)) {
          setIsSaving(false);
        }
      }
    })();
    mutationPromiseRef.current = operation;
    void operation.finally(() => {
      if (mutationPromiseRef.current === operation) {
        mutationPromiseRef.current = null;
      }
    });
    return operation;
  }, [
    applyConflict,
    applyDocument,
    canEdit,
    conflict,
    conversationId,
    document,
    draftContent,
    draftTitle,
    forgetRequestId,
    getStoredToken,
    reportError,
    requestIdFor,
    requestIsCurrent,
  ]);

  const useServerConflict = useCallback(() => {
    if (conflict) {
      applyDocument(conflict);
      setError("");
    }
  }, [applyDocument, conflict]);

  const createDocument = useCallback(
    (input: CreateCanvasDocumentInput) => {
      if (createPromiseRef.current) {
        return createPromiseRef.current;
      }
      if (isDirty || conflict) {
        setError(
          conflict
            ? "Resuelve el conflicto del borrador antes de crear otro."
            : "Guarda los cambios del borrador antes de crear otro.",
        );
        return Promise.resolve(null);
      }
      if (
        !conversationId ||
        conversation?.status !== "active" ||
        assistantBusy ||
        isLoading ||
        isSaving ||
        mutationPromiseRef.current
      ) {
        return Promise.resolve(null);
      }
      const generation = requestGenerationRef.current;
      const targetConversationId = conversationId;
      const operationKey = JSON.stringify({
        kind: "create",
        conversationId: targetConversationId,
        input,
      });
      const creationId = requestIdFor(operationKey);
      setIsLoading(true);
      setError("");
      const operation = (async () => {
        try {
          const created = await adminRequest<AssistantCanvasDocument>(
            `/assistant/conversations/${targetConversationId}/canvas/documents`,
            getStoredToken(),
            "No se pudo crear el borrador.",
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                ...input,
                creation_id: creationId,
              }),
            },
          );
          if (!requestIsCurrent(generation, targetConversationId)) {
            return null;
          }
          forgetRequestId(operationKey);
          applyDocument(created);
          setIsOpen(true);
          return created;
        } catch (requestError) {
          if (requestError instanceof ApiRequestError) {
            forgetRequestId(operationKey);
          }
          if (requestIsCurrent(generation, targetConversationId)) {
            reportError(requestError, "No se pudo crear el borrador.");
          }
          return null;
        } finally {
          if (requestIsCurrent(generation, targetConversationId)) {
            setIsLoading(false);
          }
        }
      })();
      createPromiseRef.current = operation;
      void operation.finally(() => {
        if (createPromiseRef.current === operation) {
          createPromiseRef.current = null;
        }
      });
      return operation;
    },
    [
      applyDocument,
      assistantBusy,
      conversation?.status,
      conversationId,
      forgetRequestId,
      getStoredToken,
      isLoading,
      isSaving,
      isDirty,
      conflict,
      reportError,
      requestIdFor,
      requestIsCurrent,
    ],
  );

  const performOpenDocument = useCallback(
    async (documentId: number) => {
      if (!conversationId) {
        return false;
      }
      if (mutationPromiseRef.current || createPromiseRef.current) {
        return false;
      }
      if (
        isDirty &&
        !window.confirm("Hay cambios sin guardar. ¿Quieres descartarlos?")
      ) {
        return false;
      }
      const generation = requestGenerationRef.current;
      const loadGeneration = ++documentLoadGenerationRef.current;
      const targetConversationId = conversationId;
      setIsOpen(true);
      setIsLoading(true);
      setError("");
      try {
        await setActiveDocument(documentId, targetConversationId);
        const nextDocument = await fetchDocument(
          documentId,
          generation,
          targetConversationId,
          loadGeneration,
        );
        return (
          documentLoadIsCurrent(
            generation,
            loadGeneration,
            targetConversationId,
          ) &&
          nextDocument.conversation_id === targetConversationId
        );
      } catch (requestError) {
        if (
          documentLoadIsCurrent(
            generation,
            loadGeneration,
            targetConversationId,
          )
        ) {
          reportError(requestError, "No se pudo abrir el borrador.");
        }
        return false;
      } finally {
        if (
          documentLoadIsCurrent(
            generation,
            loadGeneration,
            targetConversationId,
          )
        ) {
          setIsLoading(false);
        }
      }
    },
    [
      conversationId,
      documentLoadIsCurrent,
      fetchDocument,
      isDirty,
      reportError,
      setActiveDocument,
    ],
  );

  const openDocument = useCallback(
    (documentId: number) => {
      if (assistantBusy || isLoading || isSaving) {
        return Promise.resolve(false);
      }
      return enqueueDocumentNavigation(() => performOpenDocument(documentId));
    },
    [
      assistantBusy,
      enqueueDocumentNavigation,
      isLoading,
      isSaving,
      performOpenDocument,
    ],
  );

  const openCanvas = useCallback(async () => {
    if (!conversationId || assistantBusy || isLoading || isSaving) {
      return;
    }
    setIsOpen(true);
    if (document) {
      const generation = requestGenerationRef.current;
      const targetConversationId = conversationId;
      try {
        await setActiveDocument(document.id, targetConversationId);
      } catch (requestError) {
        if (requestIsCurrent(generation, targetConversationId)) {
          setIsOpen(false);
          reportError(requestError, "No se pudo abrir el lienzo.");
        }
      }
      return;
    }
    const firstDocument = workspace.documents.find(
      (candidate) => candidate.status === "draft",
    );
    if (firstDocument) {
      await openDocument(firstDocument.id);
    }
  }, [
    conversationId,
    assistantBusy,
    document,
    openDocument,
    isLoading,
    isSaving,
    reportError,
    requestIsCurrent,
    setActiveDocument,
    workspace.documents,
  ]);

  const closeCanvas = useCallback(async () => {
    if (
      mutationPromiseRef.current ||
      createPromiseRef.current ||
      isLoading ||
      isSaving ||
      assistantBusy
    ) {
      return false;
    }
    if (
      isDirty &&
      !window.confirm("Hay cambios sin guardar. ¿Quieres descartarlos y cerrar?")
    ) {
      return false;
    }
    const generation = requestGenerationRef.current;
    documentLoadGenerationRef.current += 1;
    const targetConversationId = conversationId;
    try {
      await setActiveDocument(null, targetConversationId);
      if (!requestIsCurrent(generation, targetConversationId)) {
        return false;
      }
      setIsOpen(false);
      setDocument(null);
      setDraftTitle("");
      setDraftContent("");
      setConflict(null);
      revisionLoadGenerationRef.current += 1;
      setRevisions([]);
      setIsLoadingRevisions(false);
      setWorkspace((current) => ({ ...current, active_document_id: null }));
      return true;
    } catch (requestError) {
      if (requestIsCurrent(generation, targetConversationId)) {
        reportError(requestError, "No se pudo cerrar el lienzo.");
      }
      return false;
    }
  }, [
    conversationId,
    assistantBusy,
    isDirty,
    isLoading,
    isSaving,
    reportError,
    requestIsCurrent,
    setActiveDocument,
  ]);

  const loadRevisions = useCallback(async () => {
    if (!document) {
      return [];
    }
    const generation = requestGenerationRef.current;
    const revisionLoadGeneration = ++revisionLoadGenerationRef.current;
    const targetConversationId = conversationId;
    const targetDocumentId = document.id;
    setIsLoadingRevisions(true);
    setError("");
    try {
      const nextRevisions = await adminRequest<AssistantCanvasRevision[]>(
        `/assistant/canvas/documents/${targetDocumentId}/revisions`,
        getStoredToken(),
        "No se pudo cargar el historial.",
      );
      if (
        requestIsCurrent(generation, targetConversationId) &&
        revisionLoadGeneration === revisionLoadGenerationRef.current &&
        currentDocumentIdRef.current === targetDocumentId
      ) {
        setRevisions(nextRevisions);
      }
      return nextRevisions;
    } catch (requestError) {
      if (
        requestIsCurrent(generation, targetConversationId) &&
        revisionLoadGeneration === revisionLoadGenerationRef.current
      ) {
        reportError(requestError, "No se pudo cargar el historial.");
      }
      return [];
    } finally {
      if (
        requestIsCurrent(generation, targetConversationId) &&
        revisionLoadGeneration === revisionLoadGenerationRef.current &&
        currentDocumentIdRef.current === targetDocumentId
      ) {
        setIsLoadingRevisions(false);
      }
    }
  }, [conversationId, document, getStoredToken, reportError, requestIsCurrent]);

  const restoreRevision = useCallback(
    (revisionNumber: number) => {
      if (mutationPromiseRef.current) {
        return mutationPromiseRef.current;
      }
      if (
        !document ||
        !canEdit ||
        isDirty ||
        assistantBusy ||
        isLoading ||
        isSaving
      ) {
        return Promise.resolve(false);
      }
      const generation = requestGenerationRef.current;
      const targetConversationId = conversationId;
      const targetDocumentId = document.id;
      const operationKey = JSON.stringify({
        kind: "restore",
        documentId: targetDocumentId,
        expectedRevision: document.current_revision,
        revisionNumber,
      });
      const mutationId = requestIdFor(operationKey);
      setIsSaving(true);
      setError("");
      const operation = (async () => {
        try {
          const restored = await adminRequest<AssistantCanvasDocument>(
            `/assistant/canvas/documents/${targetDocumentId}/revisions/${revisionNumber}/restore`,
            getStoredToken(),
            "No se pudo restaurar la revisión.",
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                expected_revision: document.current_revision,
                change_summary: `Restaurada la revisión ${revisionNumber}`,
                mutation_id: mutationId,
              }),
            },
          );
          if (!requestIsCurrent(generation, targetConversationId)) {
            return false;
          }
          forgetRequestId(operationKey);
          applyDocument(restored);
          await loadRevisions();
          return true;
        } catch (requestError) {
          if (requestError instanceof ApiRequestError) {
            forgetRequestId(operationKey);
          }
          if (requestIsCurrent(generation, targetConversationId)) {
            reportError(requestError, "No se pudo restaurar la revisión.");
          }
          return false;
        } finally {
          if (requestIsCurrent(generation, targetConversationId)) {
            setIsSaving(false);
          }
        }
      })();
      mutationPromiseRef.current = operation;
      void operation.finally(() => {
        if (mutationPromiseRef.current === operation) {
          mutationPromiseRef.current = null;
        }
      });
      return operation;
    },
    [
      applyDocument,
      assistantBusy,
      canEdit,
      conversationId,
      document,
      forgetRequestId,
      getStoredToken,
      isDirty,
      isLoading,
      isSaving,
      loadRevisions,
      reportError,
      requestIdFor,
      requestIsCurrent,
    ],
  );

  const archiveDocument = useCallback(() => {
    if (mutationPromiseRef.current) {
      return mutationPromiseRef.current;
    }
    if (
      !document ||
      !canEdit ||
      isDirty ||
      assistantBusy ||
      isLoading ||
      isSaving
    ) {
      return Promise.resolve(false);
    }
    if (!window.confirm("¿Archivar este borrador? Su historial se conservará.")) {
      return Promise.resolve(false);
    }
    const generation = requestGenerationRef.current;
    const targetConversationId = conversationId;
    const targetDocumentId = document.id;
    const operationKey = JSON.stringify({
      kind: "archive",
      documentId: targetDocumentId,
      expectedRevision: document.current_revision,
    });
    const mutationId = requestIdFor(operationKey);
    setIsSaving(true);
    const operation = (async () => {
      try {
        await adminRequest<AssistantCanvasDocument>(
          `/assistant/canvas/documents/${targetDocumentId}`,
          getStoredToken(),
          "No se pudo archivar el borrador.",
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              expected_revision: document.current_revision,
              status: "archived",
              change_summary: "Borrador archivado",
              mutation_id: mutationId,
            }),
          },
        );
        if (!requestIsCurrent(generation, targetConversationId)) {
          return false;
        }
        forgetRequestId(operationKey);
        setWorkspace((current) => ({
          documents: current.documents.filter(
            (candidate) => candidate.id !== targetDocumentId,
          ),
          active_document_id: null,
        }));
        setDocument(null);
        setDraftTitle("");
        setDraftContent("");
        setIsOpen(false);
        return true;
      } catch (requestError) {
        if (requestError instanceof ApiRequestError) {
          forgetRequestId(operationKey);
        }
        if (requestIsCurrent(generation, targetConversationId)) {
          reportError(requestError, "No se pudo archivar el borrador.");
        }
        return false;
      } finally {
        if (requestIsCurrent(generation, targetConversationId)) {
          setIsSaving(false);
        }
      }
    })();
    mutationPromiseRef.current = operation;
    void operation.finally(() => {
      if (mutationPromiseRef.current === operation) {
        mutationPromiseRef.current = null;
      }
    });
    return operation;
  }, [
    canEdit,
    assistantBusy,
    conversationId,
    document,
    forgetRequestId,
    getStoredToken,
    isDirty,
    isLoading,
    isSaving,
    reportError,
    requestIdFor,
    requestIsCurrent,
  ]);

  const openUiAction = useCallback(
    async (value: unknown) => {
      const action = parseAssistantCanvasAction(value);
      if (!action || action.context.conversation_id !== conversationId) {
        return false;
      }
      if (assistantBusy || isLoading || isSaving) {
        return false;
      }
      if (
        isDirty &&
        document?.id !== action.context.document_id &&
        !window.confirm(
          "Hay cambios sin guardar en el borrador actual. ¿Quieres descartarlos y abrir el otro borrador?",
        )
      ) {
        return false;
      }
      seenActionIdsRef.current.delete(action.id);
      return enqueueActionOpen(action, { allowDirtyDiscard: true });
    },
    [
      assistantBusy,
      conversationId,
      document?.id,
      enqueueActionOpen,
      isDirty,
      isLoading,
      isSaving,
    ],
  );

  return {
    conversationId,
    workspace,
    document,
    draftTitle,
    draftContent,
    revisions,
    isOpen,
    isLoading,
    isSaving,
    isLoadingRevisions,
    isDirty,
    canCreate,
    canEdit,
    assistantBusy,
    error,
    conflict,
    lastSavedAt,
    setDraftTitle,
    setDraftContent,
    createDocument,
    openDocument,
    openCanvas,
    closeCanvas,
    saveDocument,
    saveIfNeeded,
    saveOverConflict,
    useServerConflict,
    loadRevisions,
    restoreRevision,
    archiveDocument,
    openUiAction,
  };
}

export type AssistantCanvasController = ReturnType<
  typeof useAssistantCanvasController
>;
