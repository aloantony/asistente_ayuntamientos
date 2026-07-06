"use client";

import { useRef, useState } from "react";
import {
  userHasPermission,
  type AgentOfficeApprovalDecision,
  type AgentOfficeTask,
  type AssistantConversation,
  type AssistantConversationDetail,
  type AssistantConversationFolder,
  type AssistantKnowledgeProposal,
  type AssistantKnowledgeProposalStatus,
  type AssistantMemoryCategory,
  type AssistantMemoryEntry,
  type AssistantMemorySensitivity,
  type AssistantMemoryStatus,
  type AssistantStatus,
  type DocumentWorkArtifact,
  type DocumentWorkArtifactStatus,
  type Project,
  type User,
} from "../components/types";
import { adminRequest } from "./api";

type RequestErrorHandler = (
  requestError: unknown,
  setMessage: (message: string) => void,
  fallback: string,
) => void;

type UseAssistantControllerArgs = {
  getStoredToken: () => string;
  handleRequestError: RequestErrorHandler;
  currentUser?: User | null;
  onRequirementsChanged?: () => void;
};

type AssistantAudioTranscription = {
  text: string;
};

function toSummary(detail: AssistantConversationDetail): AssistantConversation {
  return {
    id: detail.id,
    title: detail.title,
    status: detail.status,
    folder_id: detail.folder_id,
    created_at: detail.created_at,
    updated_at: detail.updated_at,
  };
}

export function useAssistantController({
  getStoredToken,
  handleRequestError,
  currentUser,
  onRequirementsChanged,
}: UseAssistantControllerArgs) {
  const [assistantStatus, setAssistantStatus] =
    useState<AssistantStatus | null>(null);
  const [conversations, setConversations] = useState<AssistantConversation[]>(
    [],
  );
  const [conversationFolders, setConversationFolders] = useState<
    AssistantConversationFolder[]
  >([]);
  const [memoryEntries, setMemoryEntries] = useState<AssistantMemoryEntry[]>(
    [],
  );
  const [knowledgeProposals, setKnowledgeProposals] = useState<
    AssistantKnowledgeProposal[]
  >([]);
  const [agentOfficeTasks, setAgentOfficeTasks] = useState<AgentOfficeTask[]>(
    [],
  );
  const [documentWorkArtifacts, setDocumentWorkArtifacts] = useState<
    DocumentWorkArtifact[]
  >([]);
  const [isLoadingWorkspaceQueues, setIsLoadingWorkspaceQueues] =
    useState(false);
  const [selectedConversation, setSelectedConversation] =
    useState<AssistantConversationDetail | null>(null);
  const [draftMessage, setDraftMessage] = useState("");
  const [includeArchivedConversations, setIncludeArchivedConversations] =
    useState(false);
  const [isLoadingAssistant, setIsLoadingAssistant] = useState(false);
  const [isSendingMessage, setIsSendingMessage] = useState(false);
  const [assistantError, setAssistantError] = useState("");
  // Mirrors the selected conversation id so async callbacks can check
  // whether the user navigated away while a request was in flight.
  const selectedIdRef = useRef<number | null>(null);

  function applySelectedConversation(
    detail: AssistantConversationDetail | null,
  ) {
    selectedIdRef.current = detail?.id ?? null;
    setSelectedConversation(detail);
  }

  function clearAssistantState() {
    setAssistantStatus(null);
    setConversations([]);
    setConversationFolders([]);
    setMemoryEntries([]);
    setKnowledgeProposals([]);
    setAgentOfficeTasks([]);
    setDocumentWorkArtifacts([]);
    setIsLoadingWorkspaceQueues(false);
    applySelectedConversation(null);
    setDraftMessage("");
    setIncludeArchivedConversations(false);
    setIsLoadingAssistant(false);
    setIsSendingMessage(false);
    setAssistantError("");
  }

  function hasAnyPermission(permissionCodes: string[]) {
    if (!currentUser) {
      return false;
    }
    return permissionCodes.some((permissionCode) =>
      userHasPermission(currentUser, permissionCode),
    );
  }

  async function loadDocumentWorkArtifacts(token: string) {
    if (
      !hasAnyPermission([
        "documents.view",
        "documents.draft",
        "documents.review",
        "documents.export",
        "documents.manage",
      ])
    ) {
      return [];
    }

    const projects = await adminRequest<Project[]>(
      "/projects",
      token,
      "No se pudieron cargar los proyectos.",
    ).catch(() => []);

    const artifactResults = await Promise.all(
      projects.map(async (project) => {
        try {
          const artifacts = await adminRequest<DocumentWorkArtifact[]>(
            `/projects/${project.id}/document-work-artifacts`,
            token,
            "No se pudieron cargar los borradores documentales.",
          );
          return artifacts.map((artifact) => ({
            ...artifact,
            project_name: project.name,
          }));
        } catch {
          return [] as DocumentWorkArtifact[];
        }
      }),
    );

    return artifactResults
      .flat()
      .filter((artifact) => artifact.status !== "archived")
      .sort(
        (left, right) =>
          new Date(right.updated_at).getTime() - new Date(left.updated_at).getTime(),
      );
  }

  async function loadWorkspaceQueues(
    token: string = getStoredToken(),
    options: { showLoading?: boolean } = {},
  ) {
    if (!currentUser) {
      setMemoryEntries([]);
      setKnowledgeProposals([]);
      setAgentOfficeTasks([]);
      setDocumentWorkArtifacts([]);
      return;
    }

    if (options.showLoading) {
      setIsLoadingWorkspaceQueues(true);
    }

    try {
      const [pendingMemory, pendingKnowledge, tasks, artifacts] =
        await Promise.all([
          adminRequest<AssistantMemoryEntry[]>(
            "/assistant/memory?status=proposed",
            token,
            "No se pudieron cargar las propuestas de memoria.",
          ).catch(() => []),
          adminRequest<AssistantKnowledgeProposal[]>(
            "/assistant/knowledge-proposals?status=proposed",
            token,
            "No se pudieron cargar las propuestas de fuentes.",
          ).catch(() => []),
          adminRequest<AgentOfficeTask[]>(
            "/agent-office/tasks",
            token,
            "No se pudieron cargar las tareas supervisadas.",
          ).catch(() => []),
          loadDocumentWorkArtifacts(token),
        ]);
      setMemoryEntries(pendingMemory);
      setKnowledgeProposals(pendingKnowledge);
      setAgentOfficeTasks(tasks);
      setDocumentWorkArtifacts(artifacts);
    } finally {
      if (options.showLoading) {
        setIsLoadingWorkspaceQueues(false);
      }
    }
  }

  async function loadAssistant(
    includeArchived: boolean = includeArchivedConversations,
  ) {
    setIsLoadingAssistant(true);
    setAssistantError("");

    try {
      const token = getStoredToken();
      const conversationsPath = includeArchived
        ? "/assistant/conversations?include_archived=true"
        : "/assistant/conversations";
      const [status, conversationList, conversationFoldersList] =
        await Promise.all([
          adminRequest<AssistantStatus>(
            "/assistant/status",
            token,
            "No se pudo consultar el estado del asistente.",
          ),
          adminRequest<AssistantConversation[]>(
            conversationsPath,
            token,
            "No se pudieron cargar las conversaciones.",
          ),
          adminRequest<AssistantConversationFolder[]>(
            "/assistant/conversation-folders",
            token,
            "No se pudieron cargar las carpetas.",
          ).catch(() => []),
        ]);
      setAssistantStatus(status);
      setConversations(conversationList);
      setConversationFolders(conversationFoldersList);
      await loadWorkspaceQueues(token, { showLoading: true });
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo cargar el asistente.",
      );
    } finally {
      setIsLoadingAssistant(false);
    }
  }

  function toggleIncludeArchivedConversations(includeArchived: boolean) {
    setIncludeArchivedConversations(includeArchived);
    void loadAssistant(includeArchived);
  }

  function deselectConversation() {
    if (selectedIdRef.current === null) {
      return;
    }

    applySelectedConversation(null);
    setDraftMessage("");
  }

  async function selectConversation(conversationId: number) {
    setAssistantError("");

    try {
      const detail = await adminRequest<AssistantConversationDetail>(
        `/assistant/conversations/${conversationId}`,
        getStoredToken(),
        "No se pudo abrir la conversación.",
      );
      if (selectedIdRef.current !== detail.id) {
        setDraftMessage("");
      }
      applySelectedConversation(detail);
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo abrir la conversación.",
      );
    }
  }

  async function startConversation() {
    setAssistantError("");

    try {
      const detail = await adminRequest<AssistantConversationDetail>(
        "/assistant/conversations",
        getStoredToken(),
        "No se pudo crear la conversación.",
        { method: "POST", body: JSON.stringify({}) },
      );
      setDraftMessage("");
      applySelectedConversation(detail);
      setConversations((existing) => [toSummary(detail), ...existing]);
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo crear la conversación.",
      );
    }
  }

  async function sendMessage() {
    const content = draftMessage.trim();
    if (!content || !selectedConversation || isSendingMessage) {
      return;
    }
    const conversationId = selectedConversation.id;

    setIsSendingMessage(true);
    setAssistantError("");

    // Optimistic echo so the user sees their message while the agent works.
    setSelectedConversation((current) =>
      current && current.id === conversationId
        ? {
            ...current,
            messages: [
              ...current.messages,
              {
                id: -1,
                role: "user",
                content,
                actions: [],
                agent_key: null,
                routing: null,
                created_at: new Date().toISOString(),
              },
            ],
          }
        : current,
    );
    setDraftMessage("");

    try {
      const detail = await adminRequest<AssistantConversationDetail>(
        `/assistant/conversations/${conversationId}/messages`,
        getStoredToken(),
        "El asistente no ha podido responder.",
        { method: "POST", body: JSON.stringify({ content }) },
      );
      // Only replace the thread if the user is still on this conversation.
      setSelectedConversation((current) =>
        current && current.id === conversationId ? detail : current,
      );
      // Most recent activity goes to the head, matching the backend order.
      setConversations((existing) => [
        toSummary(detail),
        ...existing.filter((conversation) => conversation.id !== detail.id),
      ]);

      const mutatingTools = new Set(
        assistantStatus?.tools
          .filter((tool) => !tool.read_only)
          .map((tool) => tool.name) ?? [],
      );
      const hasMutatingAction = detail.messages.some((message) =>
        message.actions.some(
          (action) => action.ok && mutatingTools.has(action.tool),
        ),
      );
      if (hasMutatingAction) {
        onRequirementsChanged?.();
        void loadWorkspaceQueues(getStoredToken());
      }
    } catch (requestError) {
      // Drop the optimistic echo from this conversation only; the backend
      // may have persisted the user message, so a reload shows it again.
      setSelectedConversation((current) =>
        current && current.id === conversationId
          ? {
              ...current,
              messages: current.messages.filter((message) => message.id !== -1),
            }
          : current,
      );
      if (selectedIdRef.current === conversationId) {
        setDraftMessage(content);
      }
      handleRequestError(
        requestError,
        setAssistantError,
        "El asistente no ha podido responder.",
      );
    } finally {
      setIsSendingMessage(false);
    }
  }

  async function transcribeAudio(audio: Blob) {
    const formData = new FormData();
    formData.append("file", audio, "anacleto-audio.webm");
    const transcription = await adminRequest<AssistantAudioTranscription>(
      "/assistant/audio-transcriptions",
      getStoredToken(),
      "No se pudo transcribir el audio.",
      { method: "POST", body: formData },
    );
    return transcription.text;
  }

  async function archiveConversation(conversationId: number) {
    setAssistantError("");

    try {
      await adminRequest<AssistantConversationDetail>(
        `/assistant/conversations/${conversationId}`,
        getStoredToken(),
        "No se pudo archivar la conversación.",
        { method: "PATCH", body: JSON.stringify({ status: "archived" }) },
      );

      if (includeArchivedConversations) {
        // The archived conversation stays visible with its new status.
        await loadAssistant();
        if (selectedIdRef.current === conversationId) {
          await selectConversation(conversationId);
        }
        return;
      }

      setConversations((existing) =>
        existing.filter((conversation) => conversation.id !== conversationId),
      );
      if (selectedIdRef.current === conversationId) {
        applySelectedConversation(null);
        setDraftMessage("");
      }
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo archivar la conversación.",
      );
    }
  }

  async function restoreConversation(conversationId: number) {
    setAssistantError("");

    try {
      await adminRequest<AssistantConversationDetail>(
        `/assistant/conversations/${conversationId}`,
        getStoredToken(),
        "No se pudo restaurar la conversación.",
        { method: "PATCH", body: JSON.stringify({ status: "active" }) },
      );

      await loadAssistant();
      if (selectedIdRef.current === conversationId) {
        await selectConversation(conversationId);
      }
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo restaurar la conversación.",
      );
    }
  }

  async function renameConversation(conversationId: number, title: string) {
    const normalizedTitle = title.trim();
    if (!normalizedTitle) {
      return;
    }
    setAssistantError("");

    try {
      const detail = await adminRequest<AssistantConversationDetail>(
        `/assistant/conversations/${conversationId}`,
        getStoredToken(),
        "No se pudo renombrar la conversación.",
        { method: "PATCH", body: JSON.stringify({ title: normalizedTitle }) },
      );

      setConversations((existing) =>
        existing.map((conversation) =>
          conversation.id === detail.id ? toSummary(detail) : conversation,
        ),
      );
      setSelectedConversation((current) =>
        current && current.id === detail.id ? detail : current,
      );
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo renombrar la conversación.",
      );
      throw requestError;
    }
  }

  async function loadMemoryEntries(status: AssistantMemoryStatus = "proposed") {
    setAssistantError("");

    try {
      const entries = await adminRequest<AssistantMemoryEntry[]>(
        `/assistant/memory?status=${status}`,
        getStoredToken(),
        "No se pudieron cargar las propuestas de memoria.",
      );
      setMemoryEntries(entries);
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudieron cargar las propuestas de memoria.",
      );
    }
  }

  async function assignConversationFolder(
    conversationId: number,
    folderId: number | null,
  ) {
    setAssistantError("");
    try {
      const detail = await adminRequest<AssistantConversationDetail>(
        `/assistant/conversations/${conversationId}`,
        getStoredToken(),
        "No se pudo mover la conversación.",
        { method: "PATCH", body: JSON.stringify({ folder_id: folderId }) },
      );
      setConversations((existing) =>
        existing.map((conversation) =>
          conversation.id === detail.id ? toSummary(detail) : conversation,
        ),
      );
      setSelectedConversation((current) =>
        current && current.id === detail.id ? detail : current,
      );
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo mover la conversación.",
      );
    }
  }

  async function createConversationFolder(name: string) {
    const normalizedName = name.trim();
    if (!normalizedName) {
      return null;
    }
    setAssistantError("");
    try {
      const folder = await adminRequest<AssistantConversationFolder>(
        "/assistant/conversation-folders",
        getStoredToken(),
        "No se pudo crear la carpeta.",
        { method: "POST", body: JSON.stringify({ name: normalizedName }) },
      );
      setConversationFolders((existing) => [...existing, folder]);
      return folder;
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo crear la carpeta.",
      );
      return null;
    }
  }

  async function renameConversationFolder(folderId: number, name: string) {
    const normalizedName = name.trim();
    if (!normalizedName) {
      return;
    }
    setAssistantError("");
    try {
      const folder = await adminRequest<AssistantConversationFolder>(
        `/assistant/conversation-folders/${folderId}`,
        getStoredToken(),
        "No se pudo renombrar la carpeta.",
        { method: "PATCH", body: JSON.stringify({ name: normalizedName }) },
      );
      setConversationFolders((existing) =>
        existing.map((candidate) =>
          candidate.id === folder.id ? folder : candidate,
        ),
      );
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo renombrar la carpeta.",
      );
    }
  }

  async function deleteConversationFolder(folderId: number) {
    setAssistantError("");
    try {
      await adminRequest<void>(
        `/assistant/conversation-folders/${folderId}`,
        getStoredToken(),
        "No se pudo eliminar la carpeta.",
        { method: "DELETE" },
      );
      setConversationFolders((existing) =>
        existing.filter((folder) => folder.id !== folderId),
      );
      setConversations((existing) =>
        existing.map((conversation) =>
          conversation.folder_id === folderId
            ? { ...conversation, folder_id: null }
            : conversation,
        ),
      );
      setSelectedConversation((current) =>
        current && current.folder_id === folderId
          ? { ...current, folder_id: null }
          : current,
      );
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo eliminar la carpeta.",
      );
    }
  }

  async function updateMemoryEntry(
    entryId: number,
    updates: {
      category?: AssistantMemoryCategory;
      content?: string;
      status?: AssistantMemoryStatus;
      sensitivity?: AssistantMemorySensitivity;
      review_notes?: string;
    },
  ) {
    setAssistantError("");

    try {
      await adminRequest<AssistantMemoryEntry>(
        `/assistant/memory/${entryId}`,
        getStoredToken(),
        "No se pudo actualizar la memoria.",
        { method: "PATCH", body: JSON.stringify(updates) },
      );
      await loadWorkspaceQueues();
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo actualizar la memoria.",
      );
    }
  }

  async function updateKnowledgeProposal(
    proposalId: number,
    updates: {
      status?: AssistantKnowledgeProposalStatus;
      review_notes?: string;
    },
  ) {
    setAssistantError("");

    try {
      await adminRequest<AssistantKnowledgeProposal>(
        `/assistant/knowledge-proposals/${proposalId}`,
        getStoredToken(),
        "No se pudo actualizar la propuesta de fuente.",
        { method: "PATCH", body: JSON.stringify(updates) },
      );
      await loadWorkspaceQueues();
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo actualizar la propuesta de fuente.",
      );
    }
  }

  async function reviewAgentOfficeTask(
    taskId: number,
    decision: AgentOfficeApprovalDecision,
    notes?: string,
  ) {
    setAssistantError("");

    try {
      await adminRequest<AgentOfficeTask>(
        `/agent-office/tasks/${taskId}/approval`,
        getStoredToken(),
        "No se pudo actualizar la tarea supervisada.",
        { method: "PATCH", body: JSON.stringify({ decision, notes }) },
      );
      await loadWorkspaceQueues();
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo actualizar la tarea supervisada.",
      );
    }
  }

  async function updateDocumentWorkArtifact(
    artifactId: number,
    updates: {
      status?: DocumentWorkArtifactStatus;
      review_notes?: string;
      export_format?: string;
    },
  ) {
    setAssistantError("");

    try {
      await adminRequest<DocumentWorkArtifact>(
        `/document-work-artifacts/${artifactId}`,
        getStoredToken(),
        "No se pudo actualizar el borrador documental.",
        { method: "PATCH", body: JSON.stringify(updates) },
      );
      await loadWorkspaceQueues();
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo actualizar el borrador documental.",
      );
    }
  }

  return {
    assistantStatus,
    conversations,
    conversationFolders,
    memoryEntries,
    knowledgeProposals,
    agentOfficeTasks,
    documentWorkArtifacts,
    selectedConversation,
    draftMessage,
    includeArchivedConversations,
    isLoadingAssistant,
    isLoadingWorkspaceQueues,
    isSendingMessage,
    assistantError,
    setDraftMessage,
    loadAssistant,
    loadMemoryEntries,
    loadWorkspaceQueues,
    toggleIncludeArchivedConversations,
    selectConversation,
    deselectConversation,
    startConversation,
    sendMessage,
    transcribeAudio,
    archiveConversation,
    restoreConversation,
    renameConversation,
    assignConversationFolder,
    createConversationFolder,
    renameConversationFolder,
    deleteConversationFolder,
    updateMemoryEntry,
    updateKnowledgeProposal,
    reviewAgentOfficeTask,
    updateDocumentWorkArtifact,
    clearAssistantState,
  };
}
