"use client";

import {
  type Dispatch,
  type FormEvent,
  type SetStateAction,
  useState,
} from "react";
import type {
  Group,
  GroupDeleteResponse,
  GroupEditState,
  MembershipAction,
  MembershipResponse,
  User,
  UserDeleteResponse,
  UserEditState,
} from "../components/types";
import { adminRequest } from "./api";

type RequestErrorHandler = (
  requestError: unknown,
  setMessage: (message: string) => void,
  fallback: string,
) => void;

type UseAdminControllerArgs = {
  getStoredToken: () => string;
  handleRequestError: RequestErrorHandler;
  loadProjects: () => Promise<void>;
  user: User | null;
  setUser: Dispatch<SetStateAction<User | null>>;
  projectMembershipUserId: string;
  projectMembershipGroupId: string;
  setProjectMembershipUserId: Dispatch<SetStateAction<string>>;
  setProjectMembershipGroupId: Dispatch<SetStateAction<string>>;
};

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

export function useAdminController({
  getStoredToken,
  handleRequestError,
  loadProjects,
  user,
  setUser,
  projectMembershipUserId,
  projectMembershipGroupId,
  setProjectMembershipUserId,
  setProjectMembershipGroupId,
}: UseAdminControllerArgs) {
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
      handleRequestError(
        adminLoadError,
        setAdminError,
        "No se pudo cargar la información de administración.",
      );
    } finally {
      setIsLoadingAdmin(false);
    }
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
      handleRequestError(
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
      handleRequestError(
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
      handleRequestError(
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
      handleRequestError(
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
      handleRequestError(
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
      handleRequestError(
        deleteError,
        setGroupEditError,
        "No se pudo eliminar el grupo.",
      );
    } finally {
      setDeletingGroupId(null);
    }
  }

  async function updateMembership(action: MembershipAction) {
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
      handleRequestError(
        membershipUpdateError,
        setMembershipError,
        "No se pudo actualizar la pertenencia al grupo.",
      );
    } finally {
      setIsUpdatingMembership(false);
    }
  }

  return {
    adminUsers,
    groups,
    isLoadingAdmin,
    adminError,
    newUserEmail,
    newUserPassword,
    newUserFullName,
    newUserIsActive,
    newUserIsSuperuser,
    userFormError,
    isCreatingUser,
    userEdits,
    userEditError,
    userEditMessage,
    updatingUserId,
    deletingUserId,
    newGroupName,
    newGroupDescription,
    groupFormError,
    isCreatingGroup,
    groupEdits,
    groupEditError,
    groupEditMessage,
    updatingGroupId,
    deletingGroupId,
    membershipUserId,
    membershipGroupId,
    membershipError,
    membershipMessage,
    isUpdatingMembership,
    setNewUserEmail,
    setNewUserPassword,
    setNewUserFullName,
    setNewUserIsActive,
    setNewUserIsSuperuser,
    setNewGroupName,
    setNewGroupDescription,
    setMembershipUserId,
    setMembershipGroupId,
    clearAdminState,
    loadAdminData,
    updateUserEdit,
    updateGroupEdit,
    handleCreateUser,
    handleUpdateUser,
    handleDeleteUser,
    handleCreateGroup,
    handleUpdateGroup,
    handleDeleteGroup,
    updateMembership,
  };
}
