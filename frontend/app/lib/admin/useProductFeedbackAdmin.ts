"use client";

import { useRef, useState } from "react";
import type {
  AssistantAdminFeedback,
  AssistantAdminFeedbackPriority,
  AssistantAdminFeedbackStatus,
} from "../../components/types";
import { adminRequest, adminRequestWithTotal } from "../api";

const REVIEW_PAGE_SIZE = 25;

export type ProductFeedbackStatusFilter = AssistantAdminFeedbackStatus | "all";

export type ProductFeedbackListFilters = {
  status: ProductFeedbackStatusFilter;
  page: number;
};

export type ProductFeedbackUpdate = {
  priority?: AssistantAdminFeedbackPriority;
  status?: AssistantAdminFeedbackStatus;
  review_notes?: string | null;
};

type RequestErrorHandler = (
  requestError: unknown,
  setMessage: (message: string) => void,
  fallback: string,
) => void;

type UseProductFeedbackAdminArgs = {
  getStoredToken: () => string;
  handleRequestError: RequestErrorHandler;
};

function buildProductFeedbackQuery(filters: ProductFeedbackListFilters) {
  const params = new URLSearchParams({
    limit: String(REVIEW_PAGE_SIZE),
    offset: String((filters.page - 1) * REVIEW_PAGE_SIZE),
  });
  if (filters.status !== "all") {
    params.set("status", filters.status);
  }
  return `/assistant/admin-feedback?${params.toString()}`;
}

export function useProductFeedbackAdmin({
  getStoredToken,
  handleRequestError,
}: UseProductFeedbackAdminArgs) {
  const [feedbackItems, setFeedbackItems] = useState<AssistantAdminFeedback[]>(
    [],
  );
  const [feedbackTotal, setFeedbackTotal] = useState(0);
  const [isLoadingFeedback, setIsLoadingFeedback] = useState(false);
  const [updatingFeedbackId, setUpdatingFeedbackId] = useState<number | null>(
    null,
  );
  const [feedbackError, setFeedbackError] = useState("");
  const [feedbackMessage, setFeedbackMessage] = useState("");
  const [loadedFilters, setLoadedFilters] =
    useState<ProductFeedbackListFilters | null>(null);
  const latestLoadRequestId = useRef(0);

  async function loadFeedback(filters: ProductFeedbackListFilters) {
    const requestId = ++latestLoadRequestId.current;
    setIsLoadingFeedback(true);
    setFeedbackError("");

    try {
      const { items, total } =
        await adminRequestWithTotal<AssistantAdminFeedback[]>(
          buildProductFeedbackQuery(filters),
          getStoredToken(),
          "No se pudo cargar el feedback de producto.",
        );
      if (requestId !== latestLoadRequestId.current) {
        return;
      }
      setFeedbackItems(items);
      setFeedbackTotal(total);
      setLoadedFilters(filters);
    } catch (loadError) {
      if (requestId !== latestLoadRequestId.current) {
        return;
      }
      setFeedbackItems([]);
      setFeedbackTotal(0);
      setLoadedFilters(null);
      handleRequestError(
        loadError,
        setFeedbackError,
        "No se pudo cargar el feedback de producto.",
      );
    } finally {
      if (requestId === latestLoadRequestId.current) {
        setIsLoadingFeedback(false);
      }
    }
  }

  async function updateFeedback(
    feedbackId: number,
    updates: ProductFeedbackUpdate,
    filters: ProductFeedbackListFilters,
  ) {
    setUpdatingFeedbackId(feedbackId);
    setFeedbackError("");
    setFeedbackMessage("");

    try {
      const currentItem = feedbackItems.find((item) => item.id === feedbackId);
      if (!currentItem) {
        throw new Error("El registro ya no está disponible en esta página.");
      }
      await adminRequest<AssistantAdminFeedback>(
        `/assistant/admin-feedback/${feedbackId}`,
        getStoredToken(),
        "No se pudo guardar la revisión del feedback.",
        {
          method: "PATCH",
          body: JSON.stringify({
            ...updates,
            expected_updated_at: currentItem.updated_at,
          }),
        },
      );
      setFeedbackMessage("Revisión de producto guardada.");
      await loadFeedback(filters);
      return true;
    } catch (updateError) {
      handleRequestError(
        updateError,
        setFeedbackError,
        "No se pudo guardar la revisión del feedback.",
      );
      return false;
    } finally {
      setUpdatingFeedbackId(null);
    }
  }

  return {
    adminPageSize: REVIEW_PAGE_SIZE,
    feedbackItems,
    feedbackTotal,
    isLoadingFeedback,
    updatingFeedbackId,
    feedbackError,
    feedbackMessage,
    loadedFilters,
    loadFeedback,
    updateFeedback,
  };
}
