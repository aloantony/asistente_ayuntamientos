"use client";

import { FormEvent, useEffect, useState } from "react";

type User = {
  id: number;
  email: string;
  full_name: string;
  is_active: boolean;
  is_superuser: boolean;
  groups?: UserGroupSummary[];
};

type Group = {
  id: number;
  name: string;
  description: string | null;
  users?: GroupUserSummary[];
};

type ProjectStatus = "active" | "paused" | "completed" | "archived";

type Project = {
  id: number;
  name: string;
  description: string | null;
  status: ProjectStatus;
  users: GroupUserSummary[];
  groups: UserGroupSummary[];
  created_at: string;
  updated_at: string;
};

type UserGroupSummary = {
  id: number;
  name: string;
};

type GroupUserSummary = {
  id: number;
  email: string;
  full_name: string;
};

type LoginResponse = {
  access_token: string;
  token_type: string;
};

type MembershipResponse = {
  group_id: number;
  user_id: number;
  detail: string;
};

type UserDeleteResponse = {
  user_id: number;
  detail: string;
};

type GroupDeleteResponse = {
  group_id: number;
  detail: string;
};

type UserEditState = {
  full_name: string;
  is_active: boolean;
  is_superuser: boolean;
};

type GroupEditState = {
  name: string;
  description: string;
};

type ProjectEditState = {
  name: string;
  description: string;
  status: ProjectStatus;
};

class ApiRequestError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
  }
}

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

const PROJECT_STATUSES: ProjectStatus[] = [
  "active",
  "paused",
  "completed",
  "archived",
];

const PROJECT_STATUS_LABELS: Record<ProjectStatus, string> = {
  active: "Activo",
  paused: "Pausado",
  completed: "Completado",
  archived: "Archivado",
};

function translateApiDetail(detail: string, fallback: string) {
  switch (detail) {
    case "Incorrect email or password":
      return "No se pudo iniciar sesión. Revisa el email y la contraseña.";
    case "Inactive user":
      return "El usuario está inactivo.";
    case "Could not validate credentials":
      return "La sesión ha caducado o no es válida. Inicia sesión de nuevo.";
    case "Superuser privileges required":
      return "No tienes permisos de administración.";
    case "User already exists":
      return "Ya existe un usuario con ese email.";
    case "Group already exists":
      return "Ya existe un grupo con ese nombre.";
    case "User not found":
      return "No se encontró el usuario indicado.";
    case "Group not found":
      return "No se encontró el grupo indicado.";
    case "Project not found":
      return "No se encontró el proyecto indicado.";
    case "Project access denied":
      return "No tienes acceso a ese proyecto.";
    case "Cannot delete your own account":
      return "No puedes eliminar tu propia cuenta.";
    case "Cannot delete the last active superuser":
      return "No puedes eliminar el último superusuario activo.";
    default:
      return detail || fallback;
  }
}

async function readApiError(response: Response, fallback: string) {
  try {
    const data = (await response.json()) as { detail?: unknown };
    if (typeof data.detail === "string") {
      return translateApiDetail(data.detail, fallback);
    }
  } catch {
    // Use the fallback message when the API does not return JSON.
  }

  return fallback;
}

async function fetchCurrentUser(accessToken: string) {
  const response = await fetch(`${API_BASE_URL}/auth/me`, {
    headers: {
      Authorization: `Bearer ${accessToken}`,
    },
  });

  if (!response.ok) {
    throw new ApiRequestError(
      await readApiError(
        response,
        "La sesión ha caducado o no es válida. Inicia sesión de nuevo.",
      ),
      response.status,
    );
  }

  return (await response.json()) as User;
}

async function adminRequest<T>(
  path: string,
  accessToken: string,
  fallbackError: string,
  options: RequestInit = {},
) {
  const headers = new Headers(options.headers);
  headers.set("Authorization", `Bearer ${accessToken}`);

  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers,
  });

  if (!response.ok) {
    throw new ApiRequestError(
      await readApiError(response, fallbackError),
      response.status,
    );
  }

  return (await response.json()) as T;
}

function getErrorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

function isAuthError(error: unknown) {
  return error instanceof ApiRequestError && error.status === 401;
}

function buildUserEditState(users: User[]) {
  return users.reduce<Record<number, UserEditState>>((edits, adminUser) => {
    edits[adminUser.id] = {
      full_name: adminUser.full_name,
      is_active: adminUser.is_active,
      is_superuser: adminUser.is_superuser,
    };
    return edits;
  }, {});
}

function buildGroupEditState(groups: Group[]) {
  return groups.reduce<Record<number, GroupEditState>>((edits, group) => {
    edits[group.id] = {
      name: group.name,
      description: group.description ?? "",
    };
    return edits;
  }, {});
}

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

function formatUserOption(adminUser: User) {
  return `${adminUser.full_name} (${adminUser.email})`;
}

function formatProjectStatus(status: ProjectStatus) {
  return PROJECT_STATUS_LABELS[status];
}

export default function Home() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [user, setUser] = useState<User | null>(null);
  const [error, setError] = useState("");
  const [isLoadingSession, setIsLoadingSession] = useState(true);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const [adminUsers, setAdminUsers] = useState<User[]>([]);
  const [groups, setGroups] = useState<Group[]>([]);
  const [isLoadingAdmin, setIsLoadingAdmin] = useState(false);
  const [adminError, setAdminError] = useState("");

  const [newUserEmail, setNewUserEmail] = useState("");
  const [newUserPassword, setNewUserPassword] = useState("");
  const [newUserFullName, setNewUserFullName] = useState("");
  const [newUserIsActive, setNewUserIsActive] = useState(true);
  const [newUserIsSuperuser, setNewUserIsSuperuser] = useState(false);
  const [userFormError, setUserFormError] = useState("");
  const [isCreatingUser, setIsCreatingUser] = useState(false);
  const [userEdits, setUserEdits] = useState<Record<number, UserEditState>>(
    {},
  );
  const [userEditError, setUserEditError] = useState("");
  const [userEditMessage, setUserEditMessage] = useState("");
  const [updatingUserId, setUpdatingUserId] = useState<number | null>(null);
  const [deletingUserId, setDeletingUserId] = useState<number | null>(null);

  const [newGroupName, setNewGroupName] = useState("");
  const [newGroupDescription, setNewGroupDescription] = useState("");
  const [groupFormError, setGroupFormError] = useState("");
  const [isCreatingGroup, setIsCreatingGroup] = useState(false);
  const [groupEdits, setGroupEdits] = useState<Record<number, GroupEditState>>(
    {},
  );
  const [groupEditError, setGroupEditError] = useState("");
  const [groupEditMessage, setGroupEditMessage] = useState("");
  const [updatingGroupId, setUpdatingGroupId] = useState<number | null>(null);
  const [deletingGroupId, setDeletingGroupId] = useState<number | null>(null);

  const [membershipUserId, setMembershipUserId] = useState("");
  const [membershipGroupId, setMembershipGroupId] = useState("");
  const [membershipError, setMembershipError] = useState("");
  const [membershipMessage, setMembershipMessage] = useState("");
  const [isUpdatingMembership, setIsUpdatingMembership] = useState(false);

  const [projects, setProjects] = useState<Project[]>([]);
  const [isLoadingProjects, setIsLoadingProjects] = useState(false);
  const [projectError, setProjectError] = useState("");
  const [newProjectName, setNewProjectName] = useState("");
  const [newProjectDescription, setNewProjectDescription] = useState("");
  const [newProjectStatus, setNewProjectStatus] =
    useState<ProjectStatus>("active");
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

  function clearAdminState() {
    setAdminUsers([]);
    setGroups([]);
    setUserEdits({});
    setGroupEdits({});
    setAdminError("");
    setUserFormError("");
    setUserEditError("");
    setUserEditMessage("");
    setDeletingUserId(null);
    setGroupFormError("");
    setGroupEditError("");
    setGroupEditMessage("");
    setDeletingGroupId(null);
    setMembershipError("");
    setMembershipMessage("");
  }

  function handleSessionExpired(message: string) {
    window.localStorage.removeItem("access_token");
    setUser(null);
    setPassword("");
    clearAdminState();
    clearProjectState();
    setError(message);
  }

  function getStoredToken() {
    const token = window.localStorage.getItem("access_token");
    if (!token) {
      throw new ApiRequestError(
        "La sesión ha caducado o no es válida. Inicia sesión de nuevo.",
        401,
      );
    }

    return token;
  }

  async function loadAdminData() {
    setIsLoadingAdmin(true);
    setAdminError("");

    try {
      const token = getStoredToken();
      const [usersData, groupsData] = await Promise.all([
        adminRequest<User[]>(
          "/admin/users",
          token,
          "No se pudo cargar la lista de usuarios.",
        ),
        adminRequest<Group[]>(
          "/admin/groups",
          token,
          "No se pudo cargar la lista de grupos.",
        ),
      ]);

      setAdminUsers(usersData);
      setGroups(groupsData);
      setUserEdits(buildUserEditState(usersData));
      setGroupEdits(buildGroupEditState(groupsData));

      if (
        membershipGroupId &&
        !groupsData.some((group) => String(group.id) === membershipGroupId)
      ) {
        setMembershipGroupId("");
      }
      if (
        projectMembershipUserId &&
        !usersData.some(
          (adminUser) => String(adminUser.id) === projectMembershipUserId,
        )
      ) {
        setProjectMembershipUserId("");
      }
      if (
        projectMembershipGroupId &&
        !groupsData.some((group) => String(group.id) === projectMembershipGroupId)
      ) {
        setProjectMembershipGroupId("");
      }
    } catch (adminLoadError) {
      if (isAuthError(adminLoadError)) {
        handleSessionExpired(
          getErrorMessage(
            adminLoadError,
            "La sesión ha caducado o no es válida. Inicia sesión de nuevo.",
          ),
        );
        return;
      }

      setAdminError(
        getErrorMessage(
          adminLoadError,
          "No se pudo cargar la información de administración.",
        ),
      );
    } finally {
      setIsLoadingAdmin(false);
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
    } catch (projectsLoadError) {
      if (isAuthError(projectsLoadError)) {
        handleSessionExpired(
          getErrorMessage(
            projectsLoadError,
            "La sesión ha caducado o no es válida. Inicia sesión de nuevo.",
          ),
        );
        return;
      }

      setProjectError(
        getErrorMessage(
          projectsLoadError,
          "No se pudieron cargar los proyectos.",
        ),
      );
    } finally {
      setIsLoadingProjects(false);
    }
  }

  useEffect(() => {
    let isActive = true;
    const token = window.localStorage.getItem("access_token");

    if (!token) {
      setIsLoadingSession(false);
      return () => {
        isActive = false;
      };
    }

    fetchCurrentUser(token)
      .then((currentUser) => {
        if (isActive) {
          setUser(currentUser);
        }
      })
      .catch((sessionError: Error) => {
        window.localStorage.removeItem("access_token");
        if (isActive) {
          setError(sessionError.message);
        }
      })
      .finally(() => {
        if (isActive) {
          setIsLoadingSession(false);
        }
      });

    return () => {
      isActive = false;
    };
  }, []);

  useEffect(() => {
    if (!user?.is_superuser) {
      return;
    }

    loadAdminData();
  }, [user?.is_superuser]);

  useEffect(() => {
    if (!user) {
      return;
    }

    loadProjects();
  }, [user?.id]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setIsSubmitting(true);

    try {
      const loginResponse = await fetch(`${API_BASE_URL}/auth/login`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ email, password }),
      });

      if (!loginResponse.ok) {
        throw new ApiRequestError(
          await readApiError(
            loginResponse,
            "No se pudo iniciar sesión. Revisa el email y la contraseña.",
          ),
          loginResponse.status,
        );
      }

      const loginData = (await loginResponse.json()) as LoginResponse;
      window.localStorage.setItem("access_token", loginData.access_token);

      const currentUser = await fetchCurrentUser(loginData.access_token);
      setUser(currentUser);
      setPassword("");
      setError("");
    } catch (loginError) {
      window.localStorage.removeItem("access_token");
      setUser(null);
      clearAdminState();
      clearProjectState();
      setError(
        getErrorMessage(loginError, "No se pudo iniciar sesión."),
      );
    } finally {
      setIsSubmitting(false);
    }
  }

  function handleLogout() {
    window.localStorage.removeItem("access_token");
    setUser(null);
    setPassword("");
    setError("");
    clearAdminState();
    clearProjectState();
  }

  function handleAdminError(
    requestError: unknown,
    setMessage: (message: string) => void,
    fallback: string,
  ) {
    const message = getErrorMessage(requestError, fallback);

    if (isAuthError(requestError)) {
      handleSessionExpired(message);
      return;
    }

    setMessage(message);
  }

  function updateUserEdit(userId: number, updates: Partial<UserEditState>) {
    setUserEdits((currentEdits) => {
      const currentEdit = currentEdits[userId];
      if (!currentEdit) {
        return currentEdits;
      }

      return {
        ...currentEdits,
        [userId]: {
          ...currentEdit,
          ...updates,
        },
      };
    });
  }

  function updateGroupEdit(groupId: number, updates: Partial<GroupEditState>) {
    setGroupEdits((currentEdits) => {
      const currentEdit = currentEdits[groupId];
      if (!currentEdit) {
        return currentEdits;
      }

      return {
        ...currentEdits,
        [groupId]: {
          ...currentEdit,
          ...updates,
        },
      };
    });
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

  async function handleCreateUser(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setUserFormError("");
    setIsCreatingUser(true);

    try {
      const token = getStoredToken();
      await adminRequest<User>(
        "/admin/users",
        token,
        "No se pudo crear el usuario.",
        {
          method: "POST",
          body: JSON.stringify({
            email: newUserEmail,
            password: newUserPassword,
            full_name: newUserFullName,
            is_active: newUserIsActive,
            is_superuser: newUserIsSuperuser,
          }),
        },
      );

      setNewUserEmail("");
      setNewUserPassword("");
      setNewUserFullName("");
      setNewUserIsActive(true);
      setNewUserIsSuperuser(false);
      await loadAdminData();
    } catch (createError) {
      handleAdminError(
        createError,
        setUserFormError,
        "No se pudo crear el usuario.",
      );
    } finally {
      setIsCreatingUser(false);
    }
  }

  async function handleUpdateUser(userId: number) {
    const edit = userEdits[userId];
    if (!edit) {
      setUserEditError("No se pudo encontrar el usuario para editar.");
      return;
    }

    const fullName = edit.full_name.trim();
    if (!fullName) {
      setUserEditError("El nombre completo no puede estar vacío.");
      setUserEditMessage("");
      return;
    }

    setUserEditError("");
    setUserEditMessage("");
    setUpdatingUserId(userId);

    try {
      const token = getStoredToken();
      const updatedUser = await adminRequest<User>(
        `/admin/users/${userId}`,
        token,
        "No se pudo actualizar el usuario.",
        {
          method: "PATCH",
          body: JSON.stringify({
            full_name: fullName,
            is_active: edit.is_active,
            is_superuser: edit.is_superuser,
          }),
        },
      );

      setUserEditMessage("Usuario actualizado.");

      if (user?.id === updatedUser.id) {
        setUser(updatedUser);
      }

      if (user?.id === updatedUser.id && !updatedUser.is_superuser) {
        clearAdminState();
        return;
      }

      await loadAdminData();
    } catch (updateError) {
      handleAdminError(
        updateError,
        setUserEditError,
        "No se pudo actualizar el usuario.",
      );
    } finally {
      setUpdatingUserId(null);
    }
  }

  async function handleDeleteUser(adminUser: User) {
    setUserEditError("");
    setUserEditMessage("");

    if (user?.id === adminUser.id) {
      setUserEditError("No puedes eliminar tu propia cuenta.");
      return;
    }

    const confirmed = window.confirm(
      `¿Eliminar el usuario ${adminUser.email}? Esta acción no se puede deshacer.`,
    );
    if (!confirmed) {
      return;
    }

    setDeletingUserId(adminUser.id);

    try {
      const token = getStoredToken();
      await adminRequest<UserDeleteResponse>(
        `/admin/users/${adminUser.id}`,
        token,
        "No se pudo eliminar el usuario.",
        {
          method: "DELETE",
        },
      );

      if (membershipUserId === String(adminUser.id)) {
        setMembershipUserId("");
      }

      setUserEditMessage("Usuario eliminado.");
      await loadAdminData();
      await loadProjects();
    } catch (deleteError) {
      handleAdminError(
        deleteError,
        setUserEditError,
        "No se pudo eliminar el usuario.",
      );
    } finally {
      setDeletingUserId(null);
    }
  }

  async function handleCreateGroup(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setGroupFormError("");
    setIsCreatingGroup(true);

    try {
      const token = getStoredToken();
      await adminRequest<Group>(
        "/admin/groups",
        token,
        "No se pudo crear el grupo.",
        {
          method: "POST",
          body: JSON.stringify({
            name: newGroupName,
            description: newGroupDescription.trim() || null,
          }),
        },
      );

      setNewGroupName("");
      setNewGroupDescription("");
      await loadAdminData();
    } catch (createError) {
      handleAdminError(
        createError,
        setGroupFormError,
        "No se pudo crear el grupo.",
      );
    } finally {
      setIsCreatingGroup(false);
    }
  }

  async function handleUpdateGroup(groupId: number) {
    const edit = groupEdits[groupId];
    if (!edit) {
      setGroupEditError("No se pudo encontrar el grupo para editar.");
      return;
    }

    const name = edit.name.trim();
    if (!name) {
      setGroupEditError("El nombre del grupo no puede estar vacío.");
      setGroupEditMessage("");
      return;
    }

    setGroupEditError("");
    setGroupEditMessage("");
    setUpdatingGroupId(groupId);

    try {
      const token = getStoredToken();
      await adminRequest<Group>(
        `/admin/groups/${groupId}`,
        token,
        "No se pudo actualizar el grupo.",
        {
          method: "PATCH",
          body: JSON.stringify({
            name,
            description: edit.description.trim() || null,
          }),
        },
      );

      setGroupEditMessage("Grupo actualizado.");
      await loadAdminData();
    } catch (updateError) {
      handleAdminError(
        updateError,
        setGroupEditError,
        "No se pudo actualizar el grupo.",
      );
    } finally {
      setUpdatingGroupId(null);
    }
  }

  async function handleDeleteGroup(group: Group) {
    setGroupEditError("");
    setGroupEditMessage("");

    const confirmed = window.confirm(
      `¿Eliminar el grupo ${group.name}? Esta acción no se puede deshacer.`,
    );
    if (!confirmed) {
      return;
    }

    setDeletingGroupId(group.id);

    try {
      const token = getStoredToken();
      await adminRequest<GroupDeleteResponse>(
        `/admin/groups/${group.id}`,
        token,
        "No se pudo eliminar el grupo.",
        {
          method: "DELETE",
        },
      );

      if (membershipGroupId === String(group.id)) {
        setMembershipGroupId("");
      }

      setMembershipError("");
      setMembershipMessage("");
      setGroupEditMessage("Grupo eliminado.");
      await loadAdminData();
      await loadProjects();
    } catch (deleteError) {
      handleAdminError(
        deleteError,
        setGroupEditError,
        "No se pudo eliminar el grupo.",
      );
    } finally {
      setDeletingGroupId(null);
    }
  }

  async function updateMembership(action: "add" | "remove") {
    setMembershipError("");
    setMembershipMessage("");

    const userId = Number.parseInt(membershipUserId, 10);
    const groupId = Number.parseInt(membershipGroupId, 10);

    if (!Number.isInteger(userId) || !Number.isInteger(groupId)) {
      setMembershipError("Selecciona un usuario y un grupo.");
      return;
    }

    setIsUpdatingMembership(true);

    try {
      const token = getStoredToken();
      await adminRequest<MembershipResponse>(
        `/admin/groups/${groupId}/users/${userId}`,
        token,
        "No se pudo actualizar la pertenencia al grupo.",
        {
          method: action === "add" ? "POST" : "DELETE",
        },
      );

      setMembershipMessage(
        action === "add"
          ? "Usuario añadido al grupo."
          : "Usuario eliminado del grupo.",
      );
      await loadAdminData();
    } catch (membershipUpdateError) {
      handleAdminError(
        membershipUpdateError,
        setMembershipError,
        "No se pudo actualizar la pertenencia al grupo.",
      );
    } finally {
      setIsUpdatingMembership(false);
    }
  }

  async function handleCreateProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setProjectFormError("");
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
          }),
        },
      );

      setNewProjectName("");
      setNewProjectDescription("");
      setNewProjectStatus("active");
      await loadProjects();
    } catch (createError) {
      handleAdminError(
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
          body: JSON.stringify({
            name,
            description: edit.description.trim() || null,
            status: edit.status,
          }),
        },
      );

      setProjectEditMessage("Proyecto actualizado.");
      await loadProjects();
    } catch (updateError) {
      handleAdminError(
        updateError,
        setProjectEditError,
        "No se pudo actualizar el proyecto.",
      );
    } finally {
      setUpdatingProjectId(null);
    }
  }

  async function updateProjectUserMembership(action: "add" | "remove") {
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
      handleAdminError(
        membershipUpdateError,
        setProjectMembershipError,
        "No se pudo actualizar el usuario del proyecto.",
      );
    } finally {
      setIsUpdatingProjectMembership(false);
    }
  }

  async function updateProjectGroupMembership(action: "add" | "remove") {
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
      handleAdminError(
        membershipUpdateError,
        setProjectMembershipError,
        "No se pudo actualizar el grupo del proyecto.",
      );
    } finally {
      setIsUpdatingProjectMembership(false);
    }
  }

  if (isLoadingSession) {
    return (
      <main className="page">
        <section className="panel">
          <p className="eyebrow">Plataforma privada municipal</p>
          <h1>Comprobando sesión</h1>
          <p className="muted">Validando tus credenciales guardadas.</p>
        </section>
      </main>
    );
  }

  if (user) {
    return (
      <main className="page app-page">
        <div className="workspace">
          <section className="panel">
            <div className="panel-header">
              <div>
                <p className="eyebrow">Panel privado</p>
                <h1>Dashboard</h1>
              </div>
              <button
                className="secondary-button"
                type="button"
                onClick={handleLogout}
              >
                Cerrar sesión
              </button>
            </div>

            <dl className="user-details">
              <div>
                <dt>Email</dt>
                <dd>{user.email}</dd>
              </div>
              <div>
                <dt>Nombre completo</dt>
                <dd>{user.full_name}</dd>
              </div>
              <div>
                <dt>Superusuario</dt>
                <dd>{user.is_superuser ? "Sí" : "No"}</dd>
              </div>
            </dl>
          </section>

          <section className="panel admin-panel">
            <div className="panel-header">
              <div>
                <p className="eyebrow">Trabajo</p>
                <h2>Proyectos</h2>
              </div>
              <button
                className="secondary-button"
                type="button"
                onClick={loadProjects}
                disabled={isLoadingProjects}
              >
                {isLoadingProjects ? "Cargando..." : "Actualizar"}
              </button>
            </div>

            {projectError ? (
              <p className="error-message">{projectError}</p>
            ) : null}

            {!user.is_superuser && !isLoadingProjects && projects.length === 0 ? (
              <p className="small-muted">
                No tienes proyectos accesibles. Un administrador puede asignarte
                directamente o mediante un grupo.
              </p>
            ) : null}

            <div className="admin-section">
              <div className="section-header">
                <h3>Listado</h3>
                {isLoadingProjects ? (
                  <p className="small-muted">Cargando proyectos.</p>
                ) : null}
              </div>

              <div className="table-wrapper">
                <table>
                  <thead>
                    <tr>
                      <th>ID</th>
                      <th>Nombre</th>
                      <th>Descripción</th>
                      <th>Estado</th>
                      <th>Usuarios</th>
                      <th>Grupos</th>
                      {user.is_superuser ? <th>Acción</th> : null}
                    </tr>
                  </thead>
                  <tbody>
                    {projects.length > 0 ? (
                      projects.map((project) => {
                        const edit = projectEdits[project.id] ?? {
                          name: project.name,
                          description: project.description ?? "",
                          status: project.status,
                        };
                        const assignedUsers = project.users ?? [];
                        const assignedGroups = project.groups ?? [];

                        return (
                          <tr key={project.id}>
                            <td>{project.id}</td>
                            <td>
                              {user.is_superuser ? (
                                <input
                                  aria-label={`Nombre del proyecto ${project.name}`}
                                  className="table-input"
                                  onChange={(event) =>
                                    updateProjectEdit(project.id, {
                                      name: event.target.value,
                                    })
                                  }
                                  type="text"
                                  value={edit.name}
                                />
                              ) : (
                                <strong>{project.name}</strong>
                              )}
                            </td>
                            <td>
                              {user.is_superuser ? (
                                <textarea
                                  aria-label={`Descripción del proyecto ${project.name}`}
                                  className="table-textarea"
                                  onChange={(event) =>
                                    updateProjectEdit(project.id, {
                                      description: event.target.value,
                                    })
                                  }
                                  rows={2}
                                  value={edit.description}
                                />
                              ) : project.description ? (
                                project.description
                              ) : (
                                <span className="small-muted">
                                  Sin descripción
                                </span>
                              )}
                            </td>
                            <td>
                              {user.is_superuser ? (
                                <select
                                  aria-label={`Estado del proyecto ${project.name}`}
                                  className="table-input"
                                  onChange={(event) =>
                                    updateProjectEdit(project.id, {
                                      status: event.target
                                        .value as ProjectStatus,
                                    })
                                  }
                                  value={edit.status}
                                >
                                  {PROJECT_STATUSES.map((status) => (
                                    <option key={status} value={status}>
                                      {formatProjectStatus(status)}
                                    </option>
                                  ))}
                                </select>
                              ) : (
                                <span className="tag">
                                  {formatProjectStatus(project.status)}
                                </span>
                              )}
                            </td>
                            <td>
                              {assignedUsers.length > 0 ? (
                                <ul className="compact-list">
                                  {assignedUsers.map((projectUser) => (
                                    <li key={projectUser.id}>
                                      <strong>{projectUser.full_name}</strong>
                                      <span>{projectUser.email}</span>
                                    </li>
                                  ))}
                                </ul>
                              ) : (
                                <span className="small-muted">
                                  Sin usuarios
                                </span>
                              )}
                            </td>
                            <td>
                              {assignedGroups.length > 0 ? (
                                <div className="tag-list">
                                  {assignedGroups.map((group) => (
                                    <span className="tag" key={group.id}>
                                      {group.name}
                                    </span>
                                  ))}
                                </div>
                              ) : (
                                <span className="small-muted">Sin grupos</span>
                              )}
                            </td>
                            {user.is_superuser ? (
                              <td>
                                <button
                                  type="button"
                                  onClick={() => handleUpdateProject(project.id)}
                                  disabled={
                                    updatingProjectId === project.id ||
                                    isLoadingProjects
                                  }
                                >
                                  {updatingProjectId === project.id
                                    ? "Guardando..."
                                    : "Guardar"}
                                </button>
                              </td>
                            ) : null}
                          </tr>
                        );
                      })
                    ) : (
                      <tr>
                        <td colSpan={user.is_superuser ? 7 : 6}>
                          {user.is_superuser
                            ? "No hay proyectos para mostrar."
                            : "No tienes proyectos accesibles."}
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>

              {projectEditError ? (
                <p className="error-message">{projectEditError}</p>
              ) : null}
              {projectEditMessage ? (
                <p className="success-message">{projectEditMessage}</p>
              ) : null}
            </div>

            {user.is_superuser ? (
              <>
                <div className="admin-section">
                  <form className="admin-form" onSubmit={handleCreateProject}>
                    <h3>Crear proyecto</h3>
                    <div className="form-grid">
                      <label>
                        Nombre
                        <input
                          name="new-project-name"
                          onChange={(event) =>
                            setNewProjectName(event.target.value)
                          }
                          required
                          type="text"
                          value={newProjectName}
                        />
                      </label>

                      <label>
                        Estado
                        <select
                          name="new-project-status"
                          onChange={(event) =>
                            setNewProjectStatus(
                              event.target.value as ProjectStatus,
                            )
                          }
                          value={newProjectStatus}
                        >
                          {PROJECT_STATUSES.map((status) => (
                            <option key={status} value={status}>
                              {formatProjectStatus(status)}
                            </option>
                          ))}
                        </select>
                      </label>

                      <label>
                        Descripción
                        <textarea
                          name="new-project-description"
                          onChange={(event) =>
                            setNewProjectDescription(event.target.value)
                          }
                          rows={3}
                          value={newProjectDescription}
                        />
                      </label>
                    </div>

                    {projectFormError ? (
                      <p className="error-message">{projectFormError}</p>
                    ) : null}

                    <button type="submit" disabled={isCreatingProject}>
                      {isCreatingProject ? "Creando..." : "Crear proyecto"}
                    </button>
                  </form>
                </div>

                <div className="admin-section">
                  <h3>Pertenencia a proyectos</h3>

                  <div className="membership-controls">
                    <label>
                      Proyecto
                      <select
                        onChange={(event) =>
                          setProjectMembershipProjectId(event.target.value)
                        }
                        value={projectMembershipProjectId}
                      >
                        <option value="">Selecciona un proyecto</option>
                        {projects.map((project) => (
                          <option key={project.id} value={project.id}>
                            {project.name}
                          </option>
                        ))}
                      </select>
                    </label>

                    <label>
                      Usuario
                      <select
                        onChange={(event) =>
                          setProjectMembershipUserId(event.target.value)
                        }
                        value={projectMembershipUserId}
                      >
                        <option value="">Selecciona un usuario</option>
                        {adminUsers.map((adminUser) => (
                          <option key={adminUser.id} value={adminUser.id}>
                            {formatUserOption(adminUser)}
                          </option>
                        ))}
                      </select>
                    </label>

                    <label>
                      Grupo
                      <select
                        onChange={(event) =>
                          setProjectMembershipGroupId(event.target.value)
                        }
                        value={projectMembershipGroupId}
                      >
                        <option value="">Selecciona un grupo</option>
                        {groups.map((group) => (
                          <option key={group.id} value={group.id}>
                            {group.name}
                          </option>
                        ))}
                      </select>
                    </label>
                  </div>

                  {projectMembershipError ? (
                    <p className="error-message">{projectMembershipError}</p>
                  ) : null}
                  {projectMembershipMessage ? (
                    <p className="success-message">
                      {projectMembershipMessage}
                    </p>
                  ) : null}

                  <div className="button-row">
                    <button
                      type="button"
                      onClick={() => updateProjectUserMembership("add")}
                      disabled={isUpdatingProjectMembership}
                    >
                      Añadir usuario
                    </button>
                    <button
                      className="secondary-button"
                      type="button"
                      onClick={() => updateProjectUserMembership("remove")}
                      disabled={isUpdatingProjectMembership}
                    >
                      Quitar usuario
                    </button>
                    <button
                      type="button"
                      onClick={() => updateProjectGroupMembership("add")}
                      disabled={isUpdatingProjectMembership}
                    >
                      Añadir grupo
                    </button>
                    <button
                      className="secondary-button"
                      type="button"
                      onClick={() => updateProjectGroupMembership("remove")}
                      disabled={isUpdatingProjectMembership}
                    >
                      Quitar grupo
                    </button>
                  </div>
                </div>
              </>
            ) : null}
          </section>

          {user.is_superuser ? (
            <section className="panel admin-panel">
              <div className="panel-header">
                <div>
                  <p className="eyebrow">Administración</p>
                  <h2>Usuarios y grupos</h2>
                </div>
                <button
                  className="secondary-button"
                  type="button"
                  onClick={loadAdminData}
                  disabled={isLoadingAdmin}
                >
                  {isLoadingAdmin ? "Cargando..." : "Actualizar"}
                </button>
              </div>

              {adminError ? (
                <p className="error-message">{adminError}</p>
              ) : null}

              <div className="admin-section">
                <div className="section-header">
                  <h3>Usuarios</h3>
                  {isLoadingAdmin ? (
                    <p className="small-muted">Cargando usuarios.</p>
                  ) : null}
                </div>

                <div className="table-wrapper">
                  <table>
                    <thead>
                      <tr>
                        <th>ID</th>
                        <th>Email</th>
                        <th>Nombre completo</th>
                        <th>Grupos</th>
                        <th>Activo</th>
                        <th>Superusuario</th>
                        <th>Acción</th>
                      </tr>
                    </thead>
                    <tbody>
                      {adminUsers.length > 0 ? (
                        adminUsers.map((adminUser) => {
                          const edit = userEdits[adminUser.id] ?? {
                            full_name: adminUser.full_name,
                            is_active: adminUser.is_active,
                            is_superuser: adminUser.is_superuser,
                          };
                          const assignedGroups = adminUser.groups ?? [];
                          const isCurrentUser = user?.id === adminUser.id;

                          return (
                            <tr key={adminUser.id}>
                              <td>{adminUser.id}</td>
                              <td>{adminUser.email}</td>
                              <td>
                                <input
                                  aria-label={`Nombre completo de ${adminUser.email}`}
                                  className="table-input"
                                  onChange={(event) =>
                                    updateUserEdit(adminUser.id, {
                                      full_name: event.target.value,
                                    })
                                  }
                                  type="text"
                                  value={edit.full_name}
                                />
                              </td>
                              <td>
                                {assignedGroups.length > 0 ? (
                                  <div className="tag-list">
                                    {assignedGroups.map((group) => (
                                      <span className="tag" key={group.id}>
                                        {group.name}
                                      </span>
                                    ))}
                                  </div>
                                ) : (
                                  <span className="small-muted">Sin grupos</span>
                                )}
                              </td>
                              <td>
                                <label className="table-checkbox">
                                  <input
                                    checked={edit.is_active}
                                    onChange={(event) =>
                                      updateUserEdit(adminUser.id, {
                                        is_active: event.target.checked,
                                      })
                                    }
                                    type="checkbox"
                                  />
                                  Activo
                                </label>
                              </td>
                              <td>
                                <label className="table-checkbox">
                                  <input
                                    checked={edit.is_superuser}
                                    onChange={(event) =>
                                      updateUserEdit(adminUser.id, {
                                        is_superuser: event.target.checked,
                                      })
                                    }
                                    type="checkbox"
                                  />
                                  Superusuario
                                </label>
                              </td>
                              <td>
                                <div className="table-actions">
                                  <button
                                    type="button"
                                    onClick={() => handleUpdateUser(adminUser.id)}
                                    disabled={
                                      updatingUserId === adminUser.id ||
                                      deletingUserId === adminUser.id ||
                                      isLoadingAdmin
                                    }
                                  >
                                    {updatingUserId === adminUser.id
                                      ? "Guardando..."
                                      : "Guardar"}
                                  </button>
                                  <button
                                    className="danger-button"
                                    type="button"
                                    onClick={() => handleDeleteUser(adminUser)}
                                    disabled={
                                      isCurrentUser ||
                                      deletingUserId === adminUser.id ||
                                      updatingUserId === adminUser.id ||
                                      isLoadingAdmin
                                    }
                                    title={
                                      isCurrentUser
                                        ? "No puedes eliminar la cuenta de la sesión actual"
                                        : undefined
                                    }
                                  >
                                    {deletingUserId === adminUser.id
                                      ? "Eliminando..."
                                      : "Eliminar"}
                                  </button>
                                </div>
                              </td>
                            </tr>
                          );
                        })
                      ) : (
                        <tr>
                          <td colSpan={7}>No hay usuarios para mostrar.</td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>

                {userEditError ? (
                  <p className="error-message">{userEditError}</p>
                ) : null}
                {userEditMessage ? (
                  <p className="success-message">{userEditMessage}</p>
                ) : null}

                <form className="admin-form" onSubmit={handleCreateUser}>
                  <h4>Crear usuario</h4>
                  <div className="form-grid">
                    <label>
                      Email
                      <input
                        autoComplete="email"
                        name="new-user-email"
                        onChange={(event) =>
                          setNewUserEmail(event.target.value)
                        }
                        required
                        type="email"
                        value={newUserEmail}
                      />
                    </label>

                    <label>
                      Contraseña
                      <input
                        autoComplete="new-password"
                        minLength={8}
                        name="new-user-password"
                        onChange={(event) =>
                          setNewUserPassword(event.target.value)
                        }
                        required
                        type="password"
                        value={newUserPassword}
                      />
                    </label>

                    <label>
                      Nombre completo
                      <input
                        name="new-user-full-name"
                        onChange={(event) =>
                          setNewUserFullName(event.target.value)
                        }
                        required
                        type="text"
                        value={newUserFullName}
                      />
                    </label>
                  </div>

                  <div className="checkbox-row">
                    <label className="checkbox-label">
                      <input
                        checked={newUserIsActive}
                        onChange={(event) =>
                          setNewUserIsActive(event.target.checked)
                        }
                        type="checkbox"
                      />
                      Activo
                    </label>
                    <label className="checkbox-label">
                      <input
                        checked={newUserIsSuperuser}
                        onChange={(event) =>
                          setNewUserIsSuperuser(event.target.checked)
                        }
                        type="checkbox"
                      />
                      Superusuario
                    </label>
                  </div>

                  {userFormError ? (
                    <p className="error-message">{userFormError}</p>
                  ) : null}

                  <button type="submit" disabled={isCreatingUser}>
                    {isCreatingUser ? "Creando..." : "Crear usuario"}
                  </button>
                </form>
              </div>

              <div className="admin-section">
                <div className="section-header">
                  <h3>Grupos</h3>
                  {isLoadingAdmin ? (
                    <p className="small-muted">Cargando grupos.</p>
                  ) : null}
                </div>

                <div className="table-wrapper">
                  <table>
                    <thead>
                      <tr>
                        <th>ID</th>
                        <th>Nombre</th>
                        <th>Descripción</th>
                        <th>Usuarios</th>
                        <th>Acción</th>
                      </tr>
                    </thead>
                    <tbody>
                      {groups.length > 0 ? (
                        groups.map((group) => {
                          const edit = groupEdits[group.id] ?? {
                            name: group.name,
                            description: group.description ?? "",
                          };
                          const assignedUsers = group.users ?? [];

                          return (
                            <tr key={group.id}>
                              <td>{group.id}</td>
                              <td>
                                <input
                                  aria-label={`Nombre del grupo ${group.name}`}
                                  className="table-input"
                                  onChange={(event) =>
                                    updateGroupEdit(group.id, {
                                      name: event.target.value,
                                    })
                                  }
                                  type="text"
                                  value={edit.name}
                                />
                              </td>
                              <td>
                                <textarea
                                  aria-label={`Descripción del grupo ${group.name}`}
                                  className="table-textarea"
                                  onChange={(event) =>
                                    updateGroupEdit(group.id, {
                                      description: event.target.value,
                                    })
                                  }
                                  rows={2}
                                  value={edit.description}
                                />
                              </td>
                              <td>
                                {assignedUsers.length > 0 ? (
                                  <ul className="compact-list">
                                    {assignedUsers.map((groupUser) => (
                                      <li key={groupUser.id}>
                                        <strong>{groupUser.full_name}</strong>
                                        <span>{groupUser.email}</span>
                                      </li>
                                    ))}
                                  </ul>
                                ) : (
                                  <span className="small-muted">
                                    Sin usuarios
                                  </span>
                                )}
                              </td>
                              <td>
                                <div className="table-actions">
                                  <button
                                    type="button"
                                    onClick={() => handleUpdateGroup(group.id)}
                                    disabled={
                                      updatingGroupId === group.id ||
                                      deletingGroupId === group.id ||
                                      isLoadingAdmin
                                    }
                                  >
                                    {updatingGroupId === group.id
                                      ? "Guardando..."
                                      : "Guardar"}
                                  </button>
                                  <button
                                    className="danger-button"
                                    type="button"
                                    onClick={() => handleDeleteGroup(group)}
                                    disabled={
                                      deletingGroupId === group.id ||
                                      updatingGroupId === group.id ||
                                      isLoadingAdmin
                                    }
                                  >
                                    {deletingGroupId === group.id
                                      ? "Eliminando..."
                                      : "Eliminar"}
                                  </button>
                                </div>
                              </td>
                            </tr>
                          );
                        })
                      ) : (
                        <tr>
                          <td colSpan={5}>No hay grupos para mostrar.</td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>

                {groupEditError ? (
                  <p className="error-message">{groupEditError}</p>
                ) : null}
                {groupEditMessage ? (
                  <p className="success-message">{groupEditMessage}</p>
                ) : null}

                <form className="admin-form" onSubmit={handleCreateGroup}>
                  <h4>Crear grupo</h4>
                  <div className="form-grid">
                    <label>
                      Nombre
                      <input
                        name="new-group-name"
                        onChange={(event) =>
                          setNewGroupName(event.target.value)
                        }
                        required
                        type="text"
                        value={newGroupName}
                      />
                    </label>

                    <label>
                      Descripción
                      <textarea
                        name="new-group-description"
                        onChange={(event) =>
                          setNewGroupDescription(event.target.value)
                        }
                        rows={3}
                        value={newGroupDescription}
                      />
                    </label>
                  </div>

                  {groupFormError ? (
                    <p className="error-message">{groupFormError}</p>
                  ) : null}

                  <button type="submit" disabled={isCreatingGroup}>
                    {isCreatingGroup ? "Creando..." : "Crear grupo"}
                  </button>
                </form>
              </div>

              <div className="admin-section">
                <h3>Pertenencia a grupos</h3>

                <div className="membership-controls">
                  <label>
                    Usuario
                    <select
                      onChange={(event) =>
                        setMembershipUserId(event.target.value)
                      }
                      value={membershipUserId}
                    >
                      <option value="">Selecciona un usuario</option>
                      {adminUsers.map((adminUser) => (
                        <option key={adminUser.id} value={adminUser.id}>
                          {formatUserOption(adminUser)}
                        </option>
                      ))}
                    </select>
                  </label>

                  <label>
                    Grupo
                    <select
                      onChange={(event) =>
                        setMembershipGroupId(event.target.value)
                      }
                      value={membershipGroupId}
                    >
                      <option value="">Selecciona un grupo</option>
                      {groups.map((group) => (
                        <option key={group.id} value={group.id}>
                          {group.name}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>

                {membershipError ? (
                  <p className="error-message">{membershipError}</p>
                ) : null}
                {membershipMessage ? (
                  <p className="success-message">{membershipMessage}</p>
                ) : null}

                <div className="button-row">
                  <button
                    type="button"
                    onClick={() => updateMembership("add")}
                    disabled={isUpdatingMembership}
                  >
                    Añadir al grupo
                  </button>
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => updateMembership("remove")}
                    disabled={isUpdatingMembership}
                  >
                    Quitar del grupo
                  </button>
                </div>
              </div>
            </section>
          ) : (
            <section className="panel">
              <p className="eyebrow">Administración</p>
              <h2>Acceso restringido</h2>
              <p className="muted">No tienes permisos de administración.</p>
            </section>
          )}
        </div>
      </main>
    );
  }

  return (
    <main className="page">
      <section className="panel">
        <p className="eyebrow">Plataforma privada municipal</p>
        <h1>Iniciar sesión</h1>

        <form className="login-form" onSubmit={handleSubmit}>
          <label>
            Email
            <input
              autoComplete="email"
              name="email"
              onChange={(event) => setEmail(event.target.value)}
              required
              type="email"
              value={email}
            />
          </label>

          <label>
            Contraseña
            <input
              autoComplete="current-password"
              name="password"
              onChange={(event) => setPassword(event.target.value)}
              required
              type="password"
              value={password}
            />
          </label>

          {error ? <p className="error-message">{error}</p> : null}

          <button type="submit" disabled={isSubmitting}>
            {isSubmitting ? "Entrando..." : "Entrar"}
          </button>
        </form>
      </section>
    </main>
  );
}
