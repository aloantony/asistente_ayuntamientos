"use client";

import { useRef, useState } from "react";
import type {
  AssistantConversation,
  AssistantConversationDetail,
  AssistantMemoryCategory,
  AssistantMemoryEntry,
  AssistantMemorySensitivity,
  AssistantMemoryStatus,
  AssistantStatus,
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
  onRequirementsChanged?: () => void;
};

function toSummary(detail: AssistantConversationDetail): AssistantConversation {
  return {
    id: detail.id,
    title: detail.title,
    status: detail.status,
    created_at: detail.created_at,
    updated_at: detail.updated_at,
  };
}

export function useAssistantController({
  getStoredToken,
  handleRequestError,
  onRequirementsChanged,
}: UseAssistantControllerArgs) {
  const [assistantStatus, setAssistantStatus] =
    useState<AssistantStatus | null>(null);
  const [conversations, setConversations] = useState<AssistantConversation[]>(
    [],
  );
  const [memoryEntries, setMemoryEntries] = useState<AssistantMemoryEntry[]>(
    [],
  );
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
    setMemoryEntries([]);
    applySelectedConversation(null);
    setDraftMessage("");
    setIncludeArchivedConversations(false);
    setIsLoadingAssistant(false);
    setIsSendingMessage(false);
    setAssistantError("");
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
      const [status, conversationList, pendingMemory] = await Promise.all([
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
        adminRequest<AssistantMemoryEntry[]>(
          "/assistant/memory?status=proposed",
          token,
          "No se pudieron cargar las propuestas de memoria.",
        ),
      ]);
      setAssistantStatus(status);
      setConversations(conversationList);
      setMemoryEntries(pendingMemory);
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
      await loadMemoryEntries();
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo actualizar la memoria.",
      );
    }
  }

  return {
    assistantStatus,
    conversations,
    memoryEntries,
    selectedConversation,
    draftMessage,
    includeArchivedConversations,
    isLoadingAssistant,
    isSendingMessage,
    assistantError,
    setDraftMessage,
    loadAssistant,
    loadMemoryEntries,
    toggleIncludeArchivedConversations,
    selectConversation,
    deselectConversation,
    startConversation,
    sendMessage,
    archiveConversation,
    restoreConversation,
    renameConversation,
    updateMemoryEntry,
    clearAssistantState,
  };
}
