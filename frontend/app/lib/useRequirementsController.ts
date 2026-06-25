"use client";

import { type FormEvent, useRef, useState } from "react";
import type {
  Requirement,
  RequirementEditState,
  RequirementFormState,
  RequirementMessage,
  RequirementMessageType,
  RequirementStatus,
} from "../components/types";
import { adminRequest, adminRequestWithTotal } from "./api";

const REQUIREMENTS_PAGE_SIZE = 50;

export type RequirementListFilters = {
  organizationId: string;
  projectId: string;
  status: string;
  includeArchived: boolean;
  /** Página 1-indexada; el offset se calcula como (page - 1) * 50. */
  page: number;
};

const DEFAULT_REQUIREMENT_FILTERS: RequirementListFilters = {
  organizationId: "",
  projectId: "",
  status: "",
  includeArchived: false,
  page: 1,
};

type RequestErrorHandler = (
  requestError: unknown,
  setMessage: (message: string) => void,
  fallback: string,
) => void;

type UseRequirementsControllerArgs = {
  getStoredToken: () => string;
  handleRequestError: RequestErrorHandler;
};

const emptyRequirementForm: RequirementFormState = {
  organization_id: "",
  project_id: "",
  title: "",
  summary: "",
  problem: "",
  current_process: "",
  desired_process: "",
  affected_users: "",
  involved_documents: "",
  data_sensitivity_notes: "",
  legal_notes: "",
  acceptance_criteria: "",
  open_questions: "",
  priority: "medium",
  source_type: "manual",
};

function buildRequirementEditState(
  requirement: Requirement,
): RequirementEditState {
  return {
    project_id: requirement.project_id ? String(requirement.project_id) : "",
    title: requirement.title,
    summary: requirement.summary ?? "",
    problem: requirement.problem ?? "",
    current_process: requirement.current_process ?? "",
    desired_process: requirement.desired_process ?? "",
    affected_users: requirement.affected_users ?? "",
    involved_documents: requirement.involved_documents ?? "",
    data_sensitivity_notes: requirement.data_sensitivity_notes ?? "",
    legal_notes: requirement.legal_notes ?? "",
    acceptance_criteria: requirement.acceptance_criteria ?? "",
    open_questions: requirement.open_questions ?? "",
    priority: requirement.priority,
    source_type: requirement.source_type,
    status: requirement.status,
  };
}

function nullableText(value: string) {
  return value.trim() || null;
}

function buildRequirementPayload(
  values: RequirementFormState | RequirementEditState,
) {
  return {
    project_id: values.project_id ? Number.parseInt(values.project_id, 10) : null,
    title: values.title.trim(),
    summary: nullableText(values.summary),
    problem: nullableText(values.problem),
    current_process: nullableText(values.current_process),
    desired_process: nullableText(values.desired_process),
    affected_users: nullableText(values.affected_users),
    involved_documents: nullableText(values.involved_documents),
    data_sensitivity_notes: nullableText(values.data_sensitivity_notes),
    legal_notes: nullableText(values.legal_notes),
    acceptance_criteria: nullableText(values.acceptance_criteria),
    open_questions: nullableText(values.open_questions),
    priority: values.priority,
    source_type: values.source_type,
  };
}

export function useRequirementsController({
  getStoredToken,
  handleRequestError,
}: UseRequirementsControllerArgs) {
  const [requirements, setRequirements] = useState<Requirement[]>([]);
  const [selectedRequirement, setSelectedRequirement] =
    useState<Requirement | null>(null);
  const [requirementMessages, setRequirementMessages] = useState<
    RequirementMessage[]
  >([]);
  const [isLoadingRequirements, setIsLoadingRequirements] = useState(false);
  const [requirementTotal, setRequirementTotal] = useState(0);
  const [requirementError, setRequirementError] = useState("");
  const [requirementMessage, setRequirementMessage] = useState("");
  const [filterOrganizationId, setFilterOrganizationId] = useState("");
  const [filterProjectId, setFilterProjectId] = useState("");
  const [filterStatus, setFilterStatus] = useState("");
  const [includeArchivedRequirements, setIncludeArchivedRequirements] =
    useState(false);
  const [newRequirement, setNewRequirement] =
    useState<RequirementFormState>(emptyRequirementForm);
  const [requirementFormError, setRequirementFormError] = useState("");
  const [isCreatingRequirement, setIsCreatingRequirement] = useState(false);
  const [requirementEdit, setRequirementEdit] =
    useState<RequirementEditState | null>(null);
  const [requirementEditError, setRequirementEditError] = useState("");
  const [isUpdatingRequirement, setIsUpdatingRequirement] = useState(false);
  const [newRequirementMessageBody, setNewRequirementMessageBody] =
    useState("");
  const [newRequirementMessageType, setNewRequirementMessageType] =
    useState<RequirementMessageType>("note");
  const [requirementMessagesError, setRequirementMessagesError] = useState("");
  const [isCreatingRequirementMessage, setIsCreatingRequirementMessage] =
    useState(false);

  // Últimos filtros aplicados (vienen de la URL); los reload internos tras
  // crear/editar reutilizan exactamente la misma consulta del servidor.
  const lastFiltersRef = useRef<RequirementListFilters>(
    DEFAULT_REQUIREMENT_FILTERS,
  );

  function clearRequirementsState() {
    setRequirements([]);
    setRequirementTotal(0);
    setSelectedRequirement(null);
    setRequirementMessages([]);
    setRequirementError("");
    setRequirementMessage("");
    lastFiltersRef.current = DEFAULT_REQUIREMENT_FILTERS;
    setFilterOrganizationId("");
    setFilterProjectId("");
    setFilterStatus("");
    setIncludeArchivedRequirements(false);
    setNewRequirement(emptyRequirementForm);
    setRequirementFormError("");
    setIsCreatingRequirement(false);
    setRequirementEdit(null);
    setRequirementEditError("");
    setIsUpdatingRequirement(false);
    setNewRequirementMessageBody("");
    setNewRequirementMessageType("note");
    setRequirementMessagesError("");
    setIsCreatingRequirementMessage(false);
  }

  function updateNewRequirement(updates: Partial<RequirementFormState>) {
    setNewRequirement((current) => ({ ...current, ...updates }));
  }

  function updateRequirementEdit(updates: Partial<RequirementEditState>) {
    setRequirementEdit((current) =>
      current ? { ...current, ...updates } : current,
    );
  }

  function buildRequirementsQuery(filters: RequirementListFilters) {
    const params = new URLSearchParams();
    if (filters.organizationId) {
      params.set("organization_id", filters.organizationId);
    }
    if (filters.projectId) {
      params.set("project_id", filters.projectId);
    }
    if (filters.status) {
      params.set("status", filters.status);
    }
    if (filters.includeArchived) {
      params.set("include_archived", "true");
    }
    params.set("limit", String(REQUIREMENTS_PAGE_SIZE));
    params.set("offset", String((filters.page - 1) * REQUIREMENTS_PAGE_SIZE));

    return `/requirements?${params.toString()}`;
  }

  // El filtrado es del servidor: la lista muestra exactamente lo que devuelve
  // GET /requirements con los parámetros aplicados (que viven en la URL).
  async function loadRequirements(
    filters: RequirementListFilters = lastFiltersRef.current,
  ) {
    lastFiltersRef.current = filters;
    setIsLoadingRequirements(true);
    setRequirementError("");

    try {
      const token = getStoredToken();
      const { items: requirementsData, total } =
        await adminRequestWithTotal<Requirement[]>(
          buildRequirementsQuery(filters),
          token,
          "No se pudieron cargar las necesidades.",
        );
      setRequirements(requirementsData);
      setRequirementTotal(total);

      if (
        selectedRequirement &&
        requirementsData.some(
          (requirement) => requirement.id === selectedRequirement.id,
        )
      ) {
        const current = requirementsData.find(
          (requirement) => requirement.id === selectedRequirement.id,
        );
        if (current) {
          setSelectedRequirement(current);
          setRequirementEdit(buildRequirementEditState(current));
        }
      }
    } catch (requirementsLoadError) {
      handleRequestError(
        requirementsLoadError,
        setRequirementError,
        "No se pudieron cargar las necesidades.",
      );
    } finally {
      setIsLoadingRequirements(false);
    }
  }

  function deselectRequirement() {
    setSelectedRequirement(null);
    setRequirementEdit(null);
    setRequirementMessages([]);
  }

  async function selectRequirement(requirementId: number) {
    setRequirementError("");
    setRequirementMessagesError("");

    try {
      const token = getStoredToken();
      const [requirement, messages] = await Promise.all([
        adminRequest<Requirement>(
          `/requirements/${requirementId}`,
          token,
          "No se pudo cargar la necesidad.",
        ),
        adminRequest<RequirementMessage[]>(
          `/requirements/${requirementId}/messages`,
          token,
          "No se pudieron cargar los mensajes de la necesidad.",
        ),
      ]);

      setSelectedRequirement(requirement);
      setRequirementEdit(buildRequirementEditState(requirement));
      setRequirementMessages(messages);
    } catch (selectError) {
      handleRequestError(
        selectError,
        setRequirementError,
        "No se pudo cargar la necesidad.",
      );
    }
  }

  async function handleCreateRequirement(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setRequirementFormError("");
    setRequirementMessage("");

    const organizationId = Number.parseInt(newRequirement.organization_id, 10);
    if (!Number.isInteger(organizationId)) {
      setRequirementFormError("Selecciona una organización.");
      return;
    }
    if (!newRequirement.title.trim()) {
      setRequirementFormError("El título de la necesidad no puede estar vacío.");
      return;
    }

    setIsCreatingRequirement(true);

    try {
      const token = getStoredToken();
      const createdRequirement = await adminRequest<Requirement>(
        "/requirements",
        token,
        "No se pudo crear la necesidad.",
        {
          method: "POST",
          body: JSON.stringify({
            ...buildRequirementPayload(newRequirement),
            organization_id: organizationId,
          }),
        },
      );

      setNewRequirement(emptyRequirementForm);
      setRequirementMessage("Necesidad creada.");
      await loadRequirements();
      await selectRequirement(createdRequirement.id);
    } catch (createError) {
      handleRequestError(
        createError,
        setRequirementFormError,
        "No se pudo crear la necesidad.",
      );
    } finally {
      setIsCreatingRequirement(false);
    }
  }

  async function handleUpdateRequirement() {
    if (!selectedRequirement || !requirementEdit) {
      setRequirementEditError("Selecciona una necesidad para editar.");
      return;
    }
    if (!requirementEdit.title.trim()) {
      setRequirementEditError("El título de la necesidad no puede estar vacío.");
      return;
    }

    setRequirementEditError("");
    setRequirementMessage("");
    setIsUpdatingRequirement(true);

    try {
      const token = getStoredToken();
      const updatedRequirement = await adminRequest<Requirement>(
        `/requirements/${selectedRequirement.id}`,
        token,
        "No se pudo actualizar la necesidad.",
        {
          method: "PATCH",
          body: JSON.stringify({
            ...buildRequirementPayload(requirementEdit),
            status: requirementEdit.status,
          }),
        },
      );

      setSelectedRequirement(updatedRequirement);
      setRequirementEdit(buildRequirementEditState(updatedRequirement));
      setRequirementMessage("Necesidad actualizada.");
      await loadRequirements();
    } catch (updateError) {
      handleRequestError(
        updateError,
        setRequirementEditError,
        "No se pudo actualizar la necesidad.",
      );
    } finally {
      setIsUpdatingRequirement(false);
    }
  }

  async function handleChangeRequirementStatus(status: RequirementStatus) {
    if (!selectedRequirement) {
      setRequirementEditError("Selecciona una necesidad.");
      return;
    }

    setRequirementEditError("");
    setRequirementMessage("");
    setIsUpdatingRequirement(true);

    try {
      const token = getStoredToken();
      const updatedRequirement = await adminRequest<Requirement>(
        `/requirements/${selectedRequirement.id}`,
        token,
        "No se pudo cambiar el estado de la necesidad.",
        {
          method: "PATCH",
          body: JSON.stringify({ status }),
        },
      );

      setSelectedRequirement(updatedRequirement);
      setRequirementEdit(buildRequirementEditState(updatedRequirement));
      setRequirementMessage("Estado actualizado.");
      await loadRequirements();
    } catch (statusError) {
      handleRequestError(
        statusError,
        setRequirementEditError,
        "No se pudo cambiar el estado de la necesidad.",
      );
    } finally {
      setIsUpdatingRequirement(false);
    }
  }

  async function handleCreateRequirementMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedRequirement) {
      setRequirementMessagesError("Selecciona una necesidad.");
      return;
    }
    if (!newRequirementMessageBody.trim()) {
      setRequirementMessagesError("Escribe un mensaje.");
      return;
    }

    setRequirementMessagesError("");
    setIsCreatingRequirementMessage(true);

    try {
      const token = getStoredToken();
      await adminRequest<RequirementMessage>(
        `/requirements/${selectedRequirement.id}/messages`,
        token,
        "No se pudo añadir el mensaje.",
        {
          method: "POST",
          body: JSON.stringify({
            body: newRequirementMessageBody,
            message_type: newRequirementMessageType,
          }),
        },
      );

      setNewRequirementMessageBody("");
      const messages = await adminRequest<RequirementMessage[]>(
        `/requirements/${selectedRequirement.id}/messages`,
        token,
        "No se pudieron cargar los mensajes de la necesidad.",
      );
      setRequirementMessages(messages);
    } catch (messageError) {
      handleRequestError(
        messageError,
        setRequirementMessagesError,
        "No se pudo añadir el mensaje.",
      );
    } finally {
      setIsCreatingRequirementMessage(false);
    }
  }

  return {
    requirements,
    selectedRequirement,
    requirementMessages,
    isLoadingRequirements,
    requirementsPageSize: REQUIREMENTS_PAGE_SIZE,
    requirementTotal,
    requirementError,
    requirementMessage,
    filterOrganizationId,
    filterProjectId,
    filterStatus,
    includeArchivedRequirements,
    newRequirement,
    requirementFormError,
    isCreatingRequirement,
    requirementEdit,
    requirementEditError,
    isUpdatingRequirement,
    newRequirementMessageBody,
    newRequirementMessageType,
    requirementMessagesError,
    isCreatingRequirementMessage,
    clearRequirementsState,
    loadRequirements,
    selectRequirement,
    deselectRequirement,
    updateNewRequirement,
    updateRequirementEdit,
    setFilterOrganizationId,
    setFilterProjectId,
    setFilterStatus,
    setIncludeArchivedRequirements,
    setNewRequirementMessageBody,
    setNewRequirementMessageType,
    handleCreateRequirement,
    handleUpdateRequirement,
    handleChangeRequirementStatus,
    handleCreateRequirementMessage,
  };
}
