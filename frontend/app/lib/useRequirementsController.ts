"use client";

import { type FormEvent, useState } from "react";
import type {
  Requirement,
  RequirementEditState,
  RequirementFormState,
  RequirementMessage,
  RequirementMessageType,
  RequirementStatus,
} from "../components/types";
import { adminRequest } from "./api";

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

  function clearRequirementsState() {
    setRequirements([]);
    setSelectedRequirement(null);
    setRequirementMessages([]);
    setRequirementError("");
    setRequirementMessage("");
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

  function buildRequirementsQuery() {
    const params = new URLSearchParams();
    if (filterOrganizationId) {
      params.set("organization_id", filterOrganizationId);
    }
    if (filterProjectId) {
      params.set("project_id", filterProjectId);
    }
    if (filterStatus) {
      params.set("status", filterStatus);
    }
    if (includeArchivedRequirements) {
      params.set("include_archived", "true");
    }

    const query = params.toString();
    return query ? `/requirements?${query}` : "/requirements";
  }

  async function loadRequirements() {
    setIsLoadingRequirements(true);
    setRequirementError("");

    try {
      const token = getStoredToken();
      const requirementsData = await adminRequest<Requirement[]>(
        buildRequirementsQuery(),
        token,
        "No se pudieron cargar los requisitos.",
      );
      setRequirements(requirementsData);

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
        "No se pudieron cargar los requisitos.",
      );
    } finally {
      setIsLoadingRequirements(false);
    }
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
          "No se pudo cargar el requisito.",
        ),
        adminRequest<RequirementMessage[]>(
          `/requirements/${requirementId}/messages`,
          token,
          "No se pudieron cargar los mensajes del requisito.",
        ),
      ]);

      setSelectedRequirement(requirement);
      setRequirementEdit(buildRequirementEditState(requirement));
      setRequirementMessages(messages);
    } catch (selectError) {
      handleRequestError(
        selectError,
        setRequirementError,
        "No se pudo cargar el requisito.",
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
      setRequirementFormError("El título del requisito no puede estar vacío.");
      return;
    }

    setIsCreatingRequirement(true);

    try {
      const token = getStoredToken();
      const createdRequirement = await adminRequest<Requirement>(
        "/requirements",
        token,
        "No se pudo crear el requisito.",
        {
          method: "POST",
          body: JSON.stringify({
            ...buildRequirementPayload(newRequirement),
            organization_id: organizationId,
          }),
        },
      );

      setNewRequirement(emptyRequirementForm);
      setRequirementMessage("Requisito creado.");
      await loadRequirements();
      await selectRequirement(createdRequirement.id);
    } catch (createError) {
      handleRequestError(
        createError,
        setRequirementFormError,
        "No se pudo crear el requisito.",
      );
    } finally {
      setIsCreatingRequirement(false);
    }
  }

  async function handleUpdateRequirement() {
    if (!selectedRequirement || !requirementEdit) {
      setRequirementEditError("Selecciona un requisito para editar.");
      return;
    }
    if (!requirementEdit.title.trim()) {
      setRequirementEditError("El título del requisito no puede estar vacío.");
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
        "No se pudo actualizar el requisito.",
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
      setRequirementMessage("Requisito actualizado.");
      await loadRequirements();
    } catch (updateError) {
      handleRequestError(
        updateError,
        setRequirementEditError,
        "No se pudo actualizar el requisito.",
      );
    } finally {
      setIsUpdatingRequirement(false);
    }
  }

  async function handleChangeRequirementStatus(status: RequirementStatus) {
    if (!selectedRequirement) {
      setRequirementEditError("Selecciona un requisito.");
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
        "No se pudo cambiar el estado del requisito.",
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
        "No se pudo cambiar el estado del requisito.",
      );
    } finally {
      setIsUpdatingRequirement(false);
    }
  }

  async function handleCreateRequirementMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedRequirement) {
      setRequirementMessagesError("Selecciona un requisito.");
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
        "No se pudieron cargar los mensajes del requisito.",
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
