"use client";

import { type FormEvent, useState } from "react";
import type {
  Document,
  MembershipAction,
  Project,
  ProjectEditState,
  ProjectStatus,
  User,
} from "../components/types";
import { userHasPermission } from "../components/types";
import {
  API_BASE_URL,
  ApiRequestError,
  adminRequest,
  isAuthError,
  readApiError,
} from "./api";

type RequestErrorHandler = (
  requestError: unknown,
  setMessage: (message: string) => void,
  fallback: string,
) => void;

type UseProjectsControllerArgs = {
  getStoredToken: () => string;
  handleRequestError: RequestErrorHandler;
  user: User | null;
};

function buildProjectEditState(projects: Project[]) {
  return projects.reduce<Record<number, ProjectEditState>>((edits, project) => {
    edits[project.id] = {
      name: project.name,
      description: project.description ?? "",
      status: project.status,
    };
    return edits;
  }, {});
}

export function useProjectsController({
  getStoredToken,
  handleRequestError,
  user,
}: UseProjectsControllerArgs) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [isLoadingProjects, setIsLoadingProjects] = useState(false);
  const [projectError, setProjectError] = useState("");
  const [newProjectName, setNewProjectName] = useState("");
  const [newProjectDescription, setNewProjectDescription] = useState("");
  const [newProjectStatus, setNewProjectStatus] =
    useState<ProjectStatus>("active");
  const [newProjectOrganizationId, setNewProjectOrganizationId] = useState("");
  const [projectFormError, setProjectFormError] = useState("");
  const [isCreatingProject, setIsCreatingProject] = useState(false);
  const [projectEdits, setProjectEdits] = useState<
    Record<number, ProjectEditState>
  >({});
  const [projectEditError, setProjectEditError] = useState("");
  const [projectEditMessage, setProjectEditMessage] = useState("");
  const [updatingProjectId, setUpdatingProjectId] = useState<number | null>(
    null,
  );
  const [projectMembershipProjectId, setProjectMembershipProjectId] =
    useState("");
  const [projectMembershipUserId, setProjectMembershipUserId] = useState("");
  const [projectMembershipGroupId, setProjectMembershipGroupId] = useState("");
  const [projectMembershipError, setProjectMembershipError] = useState("");
  const [projectMembershipMessage, setProjectMembershipMessage] = useState("");
  const [isUpdatingProjectMembership, setIsUpdatingProjectMembership] =
    useState(false);
  const [projectDocuments, setProjectDocuments] = useState<
    Record<number, Document[]>
  >({});
  const [projectDocumentErrors, setProjectDocumentErrors] = useState<
    Record<number, string>
  >({});
  const [isLoadingDocuments, setIsLoadingDocuments] = useState(false);
  const [includeArchivedDocuments, setIncludeArchivedDocuments] =
    useState(false);
  const [uploadingDocumentProjectId, setUploadingDocumentProjectId] = useState<
    number | null
  >(null);
  const [archivingDocumentId, setArchivingDocumentId] = useState<number | null>(
    null,
  );
  const [documentError, setDocumentError] = useState("");
  const [documentMessage, setDocumentMessage] = useState("");

  function canUseDocumentPermission(permissionCode: string) {
    if (!user) {
      return false;
    }

    return (
      userHasPermission(user, permissionCode) ||
      userHasPermission(user, "documents.manage")
    );
  }

  function clearProjectState() {
    setProjects([]);
    setProjectEdits({});
    setProjectError("");
    setNewProjectName("");
    setNewProjectDescription("");
    setNewProjectStatus("active");
    setNewProjectOrganizationId("");
    setProjectFormError("");
    setProjectEditError("");
    setProjectEditMessage("");
    setUpdatingProjectId(null);
    setProjectMembershipProjectId("");
    setProjectMembershipUserId("");
    setProjectMembershipGroupId("");
    setProjectMembershipError("");
    setProjectMembershipMessage("");
    setProjectDocuments({});
    setProjectDocumentErrors({});
    setIsLoadingDocuments(false);
    setIncludeArchivedDocuments(false);
    setUploadingDocumentProjectId(null);
    setArchivingDocumentId(null);
    setDocumentError("");
    setDocumentMessage("");
  }

  async function loadProjectDocumentsForProjects(
    projectsToLoad: Project[] = projects,
    includeArchived = includeArchivedDocuments,
  ) {
    if (!canUseDocumentPermission("documents.view")) {
      setProjectDocuments({});
      setProjectDocumentErrors({});
      return;
    }

    setIsLoadingDocuments(true);
    setProjectDocumentErrors({});

    try {
      const token = getStoredToken();
      const documentResults = await Promise.all(
        projectsToLoad.map(async (project) => {
          const query = includeArchived ? "?include_archived=true" : "";
          try {
            const documents = await adminRequest<Document[]>(
              `/projects/${project.id}/documents${query}`,
              token,
              "No se pudieron cargar los documentos del proyecto.",
            );

            return { projectId: project.id, documents, error: "" };
          } catch (documentsLoadError) {
            if (isAuthError(documentsLoadError)) {
              throw documentsLoadError;
            }

            const message =
              documentsLoadError instanceof Error
                ? documentsLoadError.message
                : "No se pudieron cargar los documentos del proyecto.";
            return {
              projectId: project.id,
              documents: [] as Document[],
              error: message,
            };
          }
        }),
      );

      setProjectDocuments(
        documentResults.reduce<Record<number, Document[]>>(
          (documentsByProject, result) => {
            documentsByProject[result.projectId] = result.documents;
            return documentsByProject;
          },
          {},
        ),
      );
      setProjectDocumentErrors(
        documentResults.reduce<Record<number, string>>(
          (errorsByProject, result) => {
            if (result.error) {
              errorsByProject[result.projectId] = result.error;
            }
            return errorsByProject;
          },
          {},
        ),
      );
    } catch (documentsError) {
      handleRequestError(
        documentsError,
        setDocumentError,
        "No se pudieron cargar los documentos.",
      );
    } finally {
      setIsLoadingDocuments(false);
    }
  }

  async function loadProjects() {
    setIsLoadingProjects(true);
    setProjectError("");

    try {
      const token = getStoredToken();
      const projectsData = await adminRequest<Project[]>(
        "/projects",
        token,
        "No se pudieron cargar los proyectos.",
      );

      setProjects(projectsData);
      setProjectEdits(buildProjectEditState(projectsData));

      if (
        projectMembershipProjectId &&
        !projectsData.some(
          (project) => String(project.id) === projectMembershipProjectId,
        )
      ) {
        setProjectMembershipProjectId("");
      }

      await loadProjectDocumentsForProjects(projectsData);
    } catch (projectsLoadError) {
      handleRequestError(
        projectsLoadError,
        setProjectError,
        "No se pudieron cargar los proyectos.",
      );
    } finally {
      setIsLoadingProjects(false);
    }
  }

  function updateProjectEdit(
    projectId: number,
    updates: Partial<ProjectEditState>,
  ) {
    setProjectEdits((currentEdits) => {
      const currentEdit = currentEdits[projectId];
      if (!currentEdit) {
        return currentEdits;
      }

      return {
        ...currentEdits,
        [projectId]: {
          ...currentEdit,
          ...updates,
        },
      };
    });
  }

  async function handleCreateProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setProjectFormError("");

    const organizationId = Number.parseInt(newProjectOrganizationId, 10);
    if (!Number.isInteger(organizationId)) {
      setProjectFormError("Selecciona una organización para el proyecto.");
      return;
    }

    setIsCreatingProject(true);

    try {
      const token = getStoredToken();
      await adminRequest<Project>(
        "/projects",
        token,
        "No se pudo crear el proyecto.",
        {
          method: "POST",
          body: JSON.stringify({
            name: newProjectName,
            description: newProjectDescription.trim() || null,
            status: newProjectStatus,
            organization_id: organizationId,
          }),
        },
      );

      setNewProjectName("");
      setNewProjectDescription("");
      setNewProjectStatus("active");
      setNewProjectOrganizationId("");
      await loadProjects();
    } catch (createError) {
      handleRequestError(
        createError,
        setProjectFormError,
        "No se pudo crear el proyecto.",
      );
    } finally {
      setIsCreatingProject(false);
    }
  }

  async function handleUpdateProject(projectId: number) {
    const edit = projectEdits[projectId];
    if (!edit) {
      setProjectEditError("No se pudo encontrar el proyecto para editar.");
      return;
    }

    const name = edit.name.trim();
    if (!name) {
      setProjectEditError("El nombre del proyecto no puede estar vacío.");
      setProjectEditMessage("");
      return;
    }

    const project = projects.find((currentProject) => currentProject.id === projectId);
    const payload: {
      name: string;
      description: string | null;
      status?: ProjectStatus;
    } = {
      name,
      description: edit.description.trim() || null,
    };
    if (!project || edit.status !== project.status) {
      payload.status = edit.status;
    }

    setProjectEditError("");
    setProjectEditMessage("");
    setUpdatingProjectId(projectId);

    try {
      const token = getStoredToken();
      await adminRequest<Project>(
        `/projects/${projectId}`,
        token,
        "No se pudo actualizar el proyecto.",
        {
          method: "PATCH",
          body: JSON.stringify(payload),
        },
      );

      setProjectEditMessage("Proyecto actualizado.");
      await loadProjects();
    } catch (updateError) {
      handleRequestError(
        updateError,
        setProjectEditError,
        "No se pudo actualizar el proyecto.",
      );
    } finally {
      setUpdatingProjectId(null);
    }
  }

  async function updateProjectUserMembership(action: MembershipAction) {
    setProjectMembershipError("");
    setProjectMembershipMessage("");

    const projectId = Number.parseInt(projectMembershipProjectId, 10);
    const userId = Number.parseInt(projectMembershipUserId, 10);

    if (!Number.isInteger(projectId) || !Number.isInteger(userId)) {
      setProjectMembershipError("Selecciona un proyecto y un usuario.");
      return;
    }

    setIsUpdatingProjectMembership(true);

    try {
      const token = getStoredToken();
      await adminRequest<Project>(
        `/projects/${projectId}/users/${userId}`,
        token,
        "No se pudo actualizar el usuario del proyecto.",
        {
          method: action === "add" ? "POST" : "DELETE",
        },
      );

      setProjectMembershipMessage(
        action === "add"
          ? "Usuario añadido al proyecto."
          : "Usuario eliminado del proyecto.",
      );
      await loadProjects();
    } catch (membershipUpdateError) {
      handleRequestError(
        membershipUpdateError,
        setProjectMembershipError,
        "No se pudo actualizar el usuario del proyecto.",
      );
    } finally {
      setIsUpdatingProjectMembership(false);
    }
  }

  async function updateProjectGroupMembership(action: MembershipAction) {
    setProjectMembershipError("");
    setProjectMembershipMessage("");

    const projectId = Number.parseInt(projectMembershipProjectId, 10);
    const groupId = Number.parseInt(projectMembershipGroupId, 10);

    if (!Number.isInteger(projectId) || !Number.isInteger(groupId)) {
      setProjectMembershipError("Selecciona un proyecto y un grupo.");
      return;
    }

    setIsUpdatingProjectMembership(true);

    try {
      const token = getStoredToken();
      await adminRequest<Project>(
        `/projects/${projectId}/groups/${groupId}`,
        token,
        "No se pudo actualizar el grupo del proyecto.",
        {
          method: action === "add" ? "POST" : "DELETE",
        },
      );

      setProjectMembershipMessage(
        action === "add"
          ? "Grupo añadido al proyecto."
          : "Grupo eliminado del proyecto.",
      );
      await loadProjects();
    } catch (membershipUpdateError) {
      handleRequestError(
        membershipUpdateError,
        setProjectMembershipError,
        "No se pudo actualizar el grupo del proyecto.",
      );
    } finally {
      setIsUpdatingProjectMembership(false);
    }
  }

  async function handleUploadDocument(
    projectId: number,
    event: FormEvent<HTMLFormElement>,
  ) {
    event.preventDefault();
    setDocumentError("");
    setDocumentMessage("");

    const form = event.currentTarget;
    const formData = new FormData(form);
    const file = formData.get("file");
    if (!(file instanceof File) || file.size === 0) {
      setDocumentError("Selecciona un archivo para subir.");
      return;
    }

    const uploadFormData = new FormData();
    uploadFormData.set("file", file);
    setUploadingDocumentProjectId(projectId);

    try {
      const token = getStoredToken();
      await adminRequest<Document>(
        `/projects/${projectId}/documents`,
        token,
        "No se pudo subir el documento.",
        {
          method: "POST",
          body: uploadFormData,
        },
      );

      form.reset();
      setDocumentMessage("Documento subido.");
      await loadProjectDocumentsForProjects(projects);
    } catch (uploadError) {
      handleRequestError(
        uploadError,
        setDocumentError,
        "No se pudo subir el documento.",
      );
    } finally {
      setUploadingDocumentProjectId(null);
    }
  }

  async function handleDownloadDocument(document: Document) {
    setDocumentError("");
    setDocumentMessage("");

    try {
      const token = getStoredToken();
      const response = await fetch(
        `${API_BASE_URL}/documents/${document.id}/download`,
        {
          headers: {
            Authorization: `Bearer ${token}`,
          },
        },
      );

      if (!response.ok) {
        throw new ApiRequestError(
          await readApiError(response, "No se pudo descargar el documento."),
          response.status,
        );
      }

      const blob = await response.blob();
      const objectUrl = window.URL.createObjectURL(blob);
      const link = window.document.createElement("a");
      link.href = objectUrl;
      link.download = document.original_filename;
      window.document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(objectUrl);
    } catch (downloadError) {
      handleRequestError(
        downloadError,
        setDocumentError,
        "No se pudo descargar el documento.",
      );
    }
  }

  async function handleArchiveDocument(document: Document) {
    if (document.status === "archived") {
      return;
    }

    setDocumentError("");
    setDocumentMessage("");
    setArchivingDocumentId(document.id);

    try {
      const token = getStoredToken();
      await adminRequest<Document>(
        `/documents/${document.id}`,
        token,
        "No se pudo archivar el documento.",
        {
          method: "PATCH",
          body: JSON.stringify({ status: "archived" }),
        },
      );

      setDocumentMessage("Documento archivado.");
      await loadProjectDocumentsForProjects(projects);
    } catch (archiveError) {
      handleRequestError(
        archiveError,
        setDocumentError,
        "No se pudo archivar el documento.",
      );
    } finally {
      setArchivingDocumentId(null);
    }
  }

  async function handleIncludeArchivedDocumentsChange(includeArchived: boolean) {
    setIncludeArchivedDocuments(includeArchived);
    await loadProjectDocumentsForProjects(projects, includeArchived);
  }

  return {
    projects,
    isLoadingProjects,
    projectError,
    newProjectName,
    newProjectDescription,
    newProjectStatus,
    newProjectOrganizationId,
    projectFormError,
    isCreatingProject,
    projectEdits,
    projectEditError,
    projectEditMessage,
    updatingProjectId,
    projectMembershipProjectId,
    projectMembershipUserId,
    projectMembershipGroupId,
    projectMembershipError,
    projectMembershipMessage,
    isUpdatingProjectMembership,
    projectDocuments,
    projectDocumentErrors,
    isLoadingDocuments,
    includeArchivedDocuments,
    uploadingDocumentProjectId,
    archivingDocumentId,
    documentError,
    documentMessage,
    setNewProjectName,
    setNewProjectDescription,
    setNewProjectStatus,
    setNewProjectOrganizationId,
    setProjectMembershipProjectId,
    setProjectMembershipUserId,
    setProjectMembershipGroupId,
    clearProjectState,
    loadProjects,
    updateProjectEdit,
    handleCreateProject,
    handleUpdateProject,
    updateProjectUserMembership,
    updateProjectGroupMembership,
    handleUploadDocument,
    handleDownloadDocument,
    handleArchiveDocument,
    handleIncludeArchivedDocumentsChange,
  };
}
