"use client";

import { useRef, useState } from "react";
import type {
  AssistantMemoryCategory,
  AssistantMemoryEntry,
  AssistantMemorySensitivity,
  AssistantMemoryStatus,
} from "../../components/types";
import { adminRequest, adminRequestWithTotal } from "../api";

const REVIEW_PAGE_SIZE = 25;

export type MemoryReviewListFilters = {
  status: AssistantMemoryStatus;
  page: number;
};

export type MemoryReviewUpdate = {
  category?: AssistantMemoryCategory;
  content?: string;
  sensitive_approval_confirmed?: boolean;
  sensitivity?: AssistantMemorySensitivity;
  status?: AssistantMemoryStatus;
  review_notes?: string | null;
};

type RequestErrorHandler = (
  requestError: unknown,
  setMessage: (message: string) => void,
  fallback: string,
) => void;

type UseMemoryReviewAdminArgs = {
  getStoredToken: () => string;
  handleRequestError: RequestErrorHandler;
};

function buildMemoryReviewQuery(filters: MemoryReviewListFilters) {
  const params = new URLSearchParams({
    status: filters.status,
    reviewable_only: "true",
    limit: String(REVIEW_PAGE_SIZE),
    offset: String((filters.page - 1) * REVIEW_PAGE_SIZE),
  });
  return `/assistant/memory?${params.toString()}`;
}

export function useMemoryReviewAdmin({
  getStoredToken,
  handleRequestError,
}: UseMemoryReviewAdminArgs) {
  const [memoryEntries, setMemoryEntries] = useState<AssistantMemoryEntry[]>([]);
  const [memoryTotal, setMemoryTotal] = useState(0);
  const [isLoadingMemory, setIsLoadingMemory] = useState(false);
  const [updatingMemoryId, setUpdatingMemoryId] = useState<number | null>(null);
  const [memoryError, setMemoryError] = useState("");
  const [memoryMessage, setMemoryMessage] = useState("");
  const [loadedFilters, setLoadedFilters] =
    useState<MemoryReviewListFilters | null>(null);
  const latestLoadRequestId = useRef(0);

  async function loadMemoryEntries(filters: MemoryReviewListFilters) {
    const requestId = ++latestLoadRequestId.current;
    setIsLoadingMemory(true);
    setMemoryError("");

    try {
      const { items, total } =
        await adminRequestWithTotal<AssistantMemoryEntry[]>(
          buildMemoryReviewQuery(filters),
          getStoredToken(),
          "No se pudo cargar la memoria pendiente de revisión.",
        );
      if (requestId !== latestLoadRequestId.current) {
        return;
      }
      setMemoryEntries(items);
      setMemoryTotal(total);
      setLoadedFilters(filters);
    } catch (loadError) {
      if (requestId !== latestLoadRequestId.current) {
        return;
      }
      setMemoryEntries([]);
      setMemoryTotal(0);
      setLoadedFilters(null);
      handleRequestError(
        loadError,
        setMemoryError,
        "No se pudo cargar la memoria pendiente de revisión.",
      );
    } finally {
      if (requestId === latestLoadRequestId.current) {
        setIsLoadingMemory(false);
      }
    }
  }

  async function updateMemoryEntry(
    entryId: number,
    updates: MemoryReviewUpdate,
    filters: MemoryReviewListFilters,
  ) {
    setUpdatingMemoryId(entryId);
    setMemoryError("");
    setMemoryMessage("");

    try {
      const currentEntry = memoryEntries.find((entry) => entry.id === entryId);
      if (!currentEntry) {
        throw new Error("La entrada ya no está disponible en esta página.");
      }
      await adminRequest<AssistantMemoryEntry>(
        `/assistant/memory/${entryId}`,
        getStoredToken(),
        "No se pudo guardar la revisión de memoria.",
        {
          method: "PATCH",
          body: JSON.stringify({
            ...updates,
            expected_updated_at: currentEntry.updated_at,
          }),
        },
      );
      setMemoryMessage("Revisión de memoria guardada.");
      await loadMemoryEntries(filters);
      return true;
    } catch (updateError) {
      handleRequestError(
        updateError,
        setMemoryError,
        "No se pudo guardar la revisión de memoria.",
      );
      return false;
    } finally {
      setUpdatingMemoryId(null);
    }
  }

  return {
    adminPageSize: REVIEW_PAGE_SIZE,
    memoryEntries,
    memoryTotal,
    isLoadingMemory,
    updatingMemoryId,
    memoryError,
    memoryMessage,
    loadedFilters,
    loadMemoryEntries,
    updateMemoryEntry,
  };
}
