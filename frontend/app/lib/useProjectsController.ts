"use client";

import { type FormEvent, useState } from "react";
import type {
  MembershipAction,
  Project,
  ProjectEditState,
  ProjectStatus,
} from "../components/types";
import { adminRequest } from "./api";

type RequestErrorHandler = (
  requestError: unknown,
  setMessage: (message: string) => void,
  fallback: string,
) => void;

type UseProjectsControllerArgs = {
  getStoredToken: () => string;
  handleRequestError: RequestErrorHandler;
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
  };
}
