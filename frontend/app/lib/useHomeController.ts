"use client";

import { type FormEvent, useEffect, useRef, useState } from "react";
import type { LoginResponse, User } from "../components/types";
import {
  API_BASE_URL,
  ApiRequestError,
  fetchCurrentUser,
  getErrorMessage,
  isAuthError,
  readApiError,
} from "./api";
import { useAdminController } from "./useAdminController";
import { useProjectsController } from "./useProjectsController";

export function useHomeController() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [user, setUser] = useState<User | null>(null);
  const [error, setError] = useState("");
  const [isLoadingSession, setIsLoadingSession] = useState(true);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const clearAdminStateRef = useRef<() => void>(() => undefined);
  const clearProjectStateRef = useRef<() => void>(() => undefined);

  function handleSessionExpired(message: string) {
    window.localStorage.removeItem("access_token");
    setUser(null);
    setPassword("");
    clearAdminStateRef.current();
    clearProjectStateRef.current();
    setError(message);
  }

  function handleRequestError(
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

  const projectsController = useProjectsController({
    getStoredToken,
    handleRequestError,
  });
  const adminController = useAdminController({
    getStoredToken,
    handleRequestError,
    loadProjects: projectsController.loadProjects,
    user,
    setUser,
    projectMembershipUserId: projectsController.projectMembershipUserId,
    projectMembershipGroupId: projectsController.projectMembershipGroupId,
    setProjectMembershipUserId:
      projectsController.setProjectMembershipUserId,
    setProjectMembershipGroupId:
      projectsController.setProjectMembershipGroupId,
  });

  clearAdminStateRef.current = adminController.clearAdminState;
  clearProjectStateRef.current = projectsController.clearProjectState;

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

    adminController.loadAdminData();
  }, [user?.is_superuser]);

  useEffect(() => {
    if (!user) {
      return;
    }

    projectsController.loadProjects();
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
      adminController.clearAdminState();
      projectsController.clearProjectState();
      setError(getErrorMessage(loginError, "No se pudo iniciar sesión."));
    } finally {
      setIsSubmitting(false);
    }
  }

  function handleLogout() {
    window.localStorage.removeItem("access_token");
    setUser(null);
    setPassword("");
    setError("");
    adminController.clearAdminState();
    projectsController.clearProjectState();
  }

  const loginFormProps = {
    email,
    password,
    error,
    isSubmitting,
    onEmailChange: setEmail,
    onPasswordChange: setPassword,
    onSubmit: handleSubmit,
  };

  const dashboardProps = user
    ? {
        user,
        onLogout: handleLogout,
      }
    : null;

  const projectsPanelProps = user
    ? {
        user,
        projects: projectsController.projects,
        adminUsers: adminController.adminUsers,
        groups: adminController.groups,
        isLoadingProjects: projectsController.isLoadingProjects,
        projectError: projectsController.projectError,
        newProjectName: projectsController.newProjectName,
        newProjectDescription: projectsController.newProjectDescription,
        newProjectStatus: projectsController.newProjectStatus,
        projectFormError: projectsController.projectFormError,
        isCreatingProject: projectsController.isCreatingProject,
        projectEdits: projectsController.projectEdits,
        projectEditError: projectsController.projectEditError,
        projectEditMessage: projectsController.projectEditMessage,
        updatingProjectId: projectsController.updatingProjectId,
        projectMembershipProjectId:
          projectsController.projectMembershipProjectId,
        projectMembershipUserId: projectsController.projectMembershipUserId,
        projectMembershipGroupId: projectsController.projectMembershipGroupId,
        projectMembershipError: projectsController.projectMembershipError,
        projectMembershipMessage: projectsController.projectMembershipMessage,
        isUpdatingProjectMembership:
          projectsController.isUpdatingProjectMembership,
        onRefresh: projectsController.loadProjects,
        onNewProjectNameChange: projectsController.setNewProjectName,
        onNewProjectDescriptionChange:
          projectsController.setNewProjectDescription,
        onNewProjectStatusChange: projectsController.setNewProjectStatus,
        onCreateProject: projectsController.handleCreateProject,
        onUpdateProjectEdit: projectsController.updateProjectEdit,
        onUpdateProject: projectsController.handleUpdateProject,
        onProjectMembershipProjectIdChange:
          projectsController.setProjectMembershipProjectId,
        onProjectMembershipUserIdChange:
          projectsController.setProjectMembershipUserId,
        onProjectMembershipGroupIdChange:
          projectsController.setProjectMembershipGroupId,
        onUpdateProjectUserMembership:
          projectsController.updateProjectUserMembership,
        onUpdateProjectGroupMembership:
          projectsController.updateProjectGroupMembership,
      }
    : null;

  const adminPanelProps =
    user?.is_superuser
      ? {
          currentUser: user,
          adminUsers: adminController.adminUsers,
          groups: adminController.groups,
          isLoadingAdmin: adminController.isLoadingAdmin,
          adminError: adminController.adminError,
          newUserEmail: adminController.newUserEmail,
          newUserPassword: adminController.newUserPassword,
          newUserFullName: adminController.newUserFullName,
          newUserIsActive: adminController.newUserIsActive,
          newUserIsSuperuser: adminController.newUserIsSuperuser,
          userFormError: adminController.userFormError,
          isCreatingUser: adminController.isCreatingUser,
          userEdits: adminController.userEdits,
          userEditError: adminController.userEditError,
          userEditMessage: adminController.userEditMessage,
          updatingUserId: adminController.updatingUserId,
          deletingUserId: adminController.deletingUserId,
          newGroupName: adminController.newGroupName,
          newGroupDescription: adminController.newGroupDescription,
          groupFormError: adminController.groupFormError,
          isCreatingGroup: adminController.isCreatingGroup,
          groupEdits: adminController.groupEdits,
          groupEditError: adminController.groupEditError,
          groupEditMessage: adminController.groupEditMessage,
          updatingGroupId: adminController.updatingGroupId,
          deletingGroupId: adminController.deletingGroupId,
          membershipUserId: adminController.membershipUserId,
          membershipGroupId: adminController.membershipGroupId,
          membershipError: adminController.membershipError,
          membershipMessage: adminController.membershipMessage,
          isUpdatingMembership: adminController.isUpdatingMembership,
          onRefresh: adminController.loadAdminData,
          onNewUserEmailChange: adminController.setNewUserEmail,
          onNewUserPasswordChange: adminController.setNewUserPassword,
          onNewUserFullNameChange: adminController.setNewUserFullName,
          onNewUserIsActiveChange: adminController.setNewUserIsActive,
          onNewUserIsSuperuserChange:
            adminController.setNewUserIsSuperuser,
          onCreateUser: adminController.handleCreateUser,
          onUpdateUserEdit: adminController.updateUserEdit,
          onUpdateUser: adminController.handleUpdateUser,
          onDeleteUser: adminController.handleDeleteUser,
          onNewGroupNameChange: adminController.setNewGroupName,
          onNewGroupDescriptionChange:
            adminController.setNewGroupDescription,
          onCreateGroup: adminController.handleCreateGroup,
          onUpdateGroupEdit: adminController.updateGroupEdit,
          onUpdateGroup: adminController.handleUpdateGroup,
          onDeleteGroup: adminController.handleDeleteGroup,
          onMembershipUserIdChange: adminController.setMembershipUserId,
          onMembershipGroupIdChange: adminController.setMembershipGroupId,
          onUpdateMembership: adminController.updateMembership,
        }
      : null;

  return {
    isLoadingSession,
    user,
    loginFormProps,
    dashboardProps,
    projectsPanelProps,
    adminPanelProps,
  };
}
