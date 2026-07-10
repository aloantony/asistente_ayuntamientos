"use client";

import { type FormEvent, useRef, useState } from "react";
import type { Ordinance, OrdinanceEditState } from "../../components/types";
import { adminRequest, adminRequestWithTotal } from "../api";

const ADMIN_PAGE_SIZE = 50;

export type OrdinanceListFilters = {
  q: string;
  province: string;
  autonomousCommunity: string;
  topic: string;
  status: string;
  includeArchived: boolean;
  /** Página 1-indexada; el offset se calcula como (page - 1) * 50. */
  page: number;
};

const DEFAULT_ORDINANCE_FILTERS: OrdinanceListFilters = {
  q: "",
  province: "",
  autonomousCommunity: "",
  topic: "",
  status: "",
  includeArchived: false,
  page: 1,
};

function buildOrdinancesQuery(filters: OrdinanceListFilters) {
  const params = new URLSearchParams();
  if (filters.q.trim()) {
    params.set("q", filters.q.trim());
  }
  if (filters.province) {
    params.set("province", filters.province);
  }
  if (filters.autonomousCommunity) {
    params.set("autonomous_community", filters.autonomousCommunity);
  }
  if (filters.topic) {
    params.set("topic", filters.topic);
  }
  if (filters.status) {
    params.set("status", filters.status);
  }
  if (filters.includeArchived) {
    params.set("include_archived", "true");
  }
  params.set("limit", String(ADMIN_PAGE_SIZE));
  params.set("offset", String((filters.page - 1) * ADMIN_PAGE_SIZE));

  return `/ordinances?${params.toString()}`;
}

type RequestErrorHandler = (
  requestError: unknown,
  setMessage: (message: string) => void,
  fallback: string,
) => void;

type UseOrdinancesAdminArgs = {
  getStoredToken: () => string;
  handleRequestError: RequestErrorHandler;
  /**
   * view||manage: cuando es false (usuario solo-crear/editar/archivar) no se
   * recarga la lista tras las mutaciones, porque el GET daría un 403 seguro.
   */
  canList?: boolean;
};

const EMPTY_ORDINANCE_FORM: OrdinanceEditState = {
  municipality_id: "",
  document_id: "",
  title: "",
  topic: "",
  subtopic: "",
  ordinance_type: "ordinance",
  summary: "",
  source_url: "",
  official_bulletin: "",
  bulletin_number: "",
  approval_date: "",
  publication_date: "",
  effective_date: "",
  status: "unknown",
  curation_status: "pending_review",
  text_content: "",
  notes: "",
  legal_review_notes: "",
};

function createEmptyOrdinanceForm(): OrdinanceEditState {
  return { ...EMPTY_ORDINANCE_FORM };
}

function buildOrdinanceEditEntry(ordinance: Ordinance): OrdinanceEditState {
  return {
    municipality_id: String(ordinance.municipality_id),
    document_id:
      ordinance.document_id === null ? "" : String(ordinance.document_id),
    title: ordinance.title,
    topic: ordinance.topic,
    subtopic: ordinance.subtopic ?? "",
    ordinance_type: ordinance.ordinance_type,
    summary: ordinance.summary ?? "",
    source_url: ordinance.source_url ?? "",
    official_bulletin: ordinance.official_bulletin ?? "",
    bulletin_number: ordinance.bulletin_number ?? "",
    approval_date: ordinance.approval_date ?? "",
    publication_date: ordinance.publication_date ?? "",
    effective_date: ordinance.effective_date ?? "",
    status: ordinance.status,
    curation_status: ordinance.curation_status,
    text_content: ordinance.text_content ?? "",
    notes: ordinance.notes ?? "",
    legal_review_notes: ordinance.legal_review_notes ?? "",
  };
}

function buildOrdinanceEditState(ordinances: Ordinance[]) {
  return ordinances.reduce<Record<number, OrdinanceEditState>>(
    (edits, ordinance) => {
      edits[ordinance.id] = buildOrdinanceEditEntry(ordinance);
      return edits;
    },
    {},
  );
}

function parseOptionalId(value: string) {
  if (!value) {
    return null;
  }

  const parsed = Number.parseInt(value, 10);
  return Number.isInteger(parsed) ? parsed : null;
}

function parseRequiredId(value: string, fieldLabel: string) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isInteger(parsed)) {
    throw new Error(`Selecciona ${fieldLabel}.`);
  }

  return parsed;
}

function buildOrdinancePayload(edit: OrdinanceEditState) {
  return {
    municipality_id: parseRequiredId(edit.municipality_id, "un municipio"),
    document_id: parseOptionalId(edit.document_id),
    title: edit.title,
    topic: edit.topic,
    subtopic: edit.subtopic.trim() || null,
    ordinance_type: edit.ordinance_type,
    summary: edit.summary.trim() || null,
    source_url: edit.source_url.trim() || null,
    official_bulletin: edit.official_bulletin.trim() || null,
    bulletin_number: edit.bulletin_number.trim() || null,
    approval_date: edit.approval_date || null,
    publication_date: edit.publication_date || null,
    effective_date: edit.effective_date || null,
    status: edit.status,
    curation_status: edit.curation_status,
    text_content: edit.text_content.trim() || null,
    notes: edit.notes.trim() || null,
    legal_review_notes: edit.legal_review_notes.trim() || null,
  };
}

export function useOrdinancesAdmin({
  getStoredToken,
  handleRequestError,
  canList = true,
}: UseOrdinancesAdminArgs) {
  const [ordinances, setOrdinances] = useState<Ordinance[]>([]);
  const [isLoadingOrdinances, setIsLoadingOrdinances] = useState(false);
  const [ordinancesError, setOrdinancesError] = useState("");

  const [ordinanceTotal, setOrdinanceTotal] = useState(0);
  // Últimos filtros aplicados; los reload internos tras crear/editar/archivar
  // reutilizan exactamente la misma consulta que pidió la página (URL).
  const lastFiltersRef = useRef<OrdinanceListFilters>(
    DEFAULT_ORDINANCE_FILTERS,
  );
  const [newOrdinance, setNewOrdinance] =
    useState<OrdinanceEditState>(createEmptyOrdinanceForm);
  const [ordinanceFormError, setOrdinanceFormError] = useState("");
  const [isCreatingOrdinance, setIsCreatingOrdinance] = useState(false);
  const [ordinanceEdits, setOrdinanceEdits] = useState<
    Record<number, OrdinanceEditState>
  >({});
  const [ordinanceEditError, setOrdinanceEditError] = useState("");
  const [ordinanceEditMessage, setOrdinanceEditMessage] = useState("");
  const [updatingOrdinanceId, setUpdatingOrdinanceId] = useState<number | null>(
    null,
  );
  // Tracks which ordinances have their full detail (text_content) loaded;
  // list items no longer include the text, so editing requires the detail.
  const [ordinanceDetailLoaded, setOrdinanceDetailLoaded] = useState<
    Record<number, boolean>
  >({});
  const [loadingOrdinanceDetailId, setLoadingOrdinanceDetailId] = useState<
    number | null
  >(null);

  async function loadOrdinances(
    filters: OrdinanceListFilters = lastFiltersRef.current,
  ) {
    lastFiltersRef.current = filters;
    setIsLoadingOrdinances(true);
    setOrdinancesError("");

    try {
      const token = getStoredToken();
      const { items, total } = await adminRequestWithTotal<Ordinance[]>(
        buildOrdinancesQuery(filters),
        token,
        "No se pudo cargar la lista de ordenanzas.",
      );

      setOrdinances(items);
      setOrdinanceTotal(total);
      setOrdinanceEdits(buildOrdinanceEditState(items));
      setOrdinanceDetailLoaded({});
    } catch (loadError) {
      handleRequestError(
        loadError,
        setOrdinancesError,
        "No se pudo cargar la lista de ordenanzas.",
      );
    } finally {
      setIsLoadingOrdinances(false);
    }
  }

  async function handleStartOrdinanceEdit(ordinanceId: number) {
    setOrdinanceEditError("");
    setOrdinanceEditMessage("");
    setLoadingOrdinanceDetailId(ordinanceId);

    try {
      const token = getStoredToken();
      const detail = await adminRequest<Ordinance>(
        `/ordinances/${ordinanceId}`,
        token,
        "No se pudo cargar el detalle de la ordenanza.",
      );

      setOrdinanceEdits((currentEdits) => ({
        ...currentEdits,
        [ordinanceId]: buildOrdinanceEditEntry(detail),
      }));
      setOrdinanceDetailLoaded((current) => ({
        ...current,
        [ordinanceId]: true,
      }));
    } catch (detailError) {
      handleRequestError(
        detailError,
        setOrdinanceEditError,
        "No se pudo cargar el detalle de la ordenanza.",
      );
    } finally {
      setLoadingOrdinanceDetailId(null);
    }
  }

  function updateNewOrdinance(updates: Partial<OrdinanceEditState>) {
    setNewOrdinance((currentOrdinance) => ({
      ...currentOrdinance,
      ...updates,
    }));
  }

  function updateOrdinanceEdit(
    ordinanceId: number,
    updates: Partial<OrdinanceEditState>,
  ) {
    setOrdinanceEdits((currentEdits) => {
      const currentEdit = currentEdits[ordinanceId];
      if (!currentEdit) {
        return currentEdits;
      }

      return {
        ...currentEdits,
        [ordinanceId]: {
          ...currentEdit,
          ...updates,
        },
      };
    });
  }

  async function handleCreateOrdinance(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setOrdinanceFormError("");
    setIsCreatingOrdinance(true);

    try {
      const token = getStoredToken();
      await adminRequest<Ordinance>(
        "/ordinances",
        token,
        "No se pudo crear la ordenanza.",
        {
          method: "POST",
          body: JSON.stringify(buildOrdinancePayload(newOrdinance)),
        },
      );

      setNewOrdinance(createEmptyOrdinanceForm());
      if (canList) {
        await loadOrdinances();
      }
    } catch (createError) {
      handleRequestError(
        createError,
        setOrdinanceFormError,
        "No se pudo crear la ordenanza.",
      );
    } finally {
      setIsCreatingOrdinance(false);
    }
  }

  async function handleUpdateOrdinance(ordinanceId: number) {
    const edit = ordinanceEdits[ordinanceId];
    if (!edit) {
      setOrdinanceEditError("No se pudo encontrar la ordenanza para editar.");
      return;
    }

    setOrdinanceEditError("");
    setOrdinanceEditMessage("");
    setUpdatingOrdinanceId(ordinanceId);

    try {
      const token = getStoredToken();
      await adminRequest<Ordinance>(
        `/ordinances/${ordinanceId}`,
        token,
        "No se pudo actualizar la ordenanza.",
        {
          method: "PATCH",
          body: JSON.stringify(buildOrdinancePayload(edit)),
        },
      );

      setOrdinanceEditMessage("Ordenanza actualizada.");
      if (canList) {
        await loadOrdinances();
      }
    } catch (updateError) {
      handleRequestError(
        updateError,
        setOrdinanceEditError,
        "No se pudo actualizar la ordenanza.",
      );
    } finally {
      setUpdatingOrdinanceId(null);
    }
  }

  async function handleArchiveOrdinance(ordinance: Ordinance) {
    setOrdinanceEditError("");
    setOrdinanceEditMessage("");

    if (ordinance.status === "archived") {
      setOrdinanceEditError("La ordenanza ya está archivada.");
      return;
    }

    const confirmed = window.confirm(
      `¿Archivar la ordenanza ${ordinance.title}? No se eliminará definitivamente.`,
    );
    if (!confirmed) {
      return;
    }

    setUpdatingOrdinanceId(ordinance.id);

    try {
      const token = getStoredToken();
      await adminRequest<Ordinance>(
        `/ordinances/${ordinance.id}`,
        token,
        "No se pudo archivar la ordenanza.",
        {
          method: "PATCH",
          body: JSON.stringify({ status: "archived" }),
        },
      );

      setOrdinanceEditMessage("Ordenanza archivada.");
      if (canList) {
        await loadOrdinances();
      }
    } catch (archiveError) {
      handleRequestError(
        archiveError,
        setOrdinanceEditError,
        "No se pudo archivar la ordenanza.",
      );
    } finally {
      setUpdatingOrdinanceId(null);
    }
  }

  return {
    ordinances,
    isLoadingOrdinances,
    ordinancesError,
    adminPageSize: ADMIN_PAGE_SIZE,
    ordinanceTotal,
    newOrdinance,
    ordinanceFormError,
    isCreatingOrdinance,
    ordinanceEdits,
    ordinanceEditError,
    ordinanceEditMessage,
    updatingOrdinanceId,
    ordinanceDetailLoaded,
    loadingOrdinanceDetailId,
    loadOrdinances,
    handleStartOrdinanceEdit,
    updateNewOrdinance,
    updateOrdinanceEdit,
    handleCreateOrdinance,
    handleUpdateOrdinance,
    handleArchiveOrdinance,
  };
}
