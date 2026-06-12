"use client";

import { type FormEvent, useState } from "react";
import type {
  Group,
  GroupDeleteResponse,
  GroupEditState,
  MembershipAction,
  MembershipResponse,
} from "../../components/types";
import { adminRequest } from "../api";

type RequestErrorHandler = (
  requestError: unknown,
  setMessage: (message: string) => void,
  fallback: string,
) => void;

type UseGroupsAdminArgs = {
  getStoredToken: () => string;
  handleRequestError: RequestErrorHandler;
};

function buildGroupEditState(groups: Group[]) {
  return groups.reduce<Record<number, GroupEditState>>((edits, group) => {
    edits[group.id] = {
      name: group.name,
      description: group.description ?? "",
      organization_id: group.organization_id,
    };
    return edits;
  }, {});
}

export function useGroupsAdmin({
  getStoredToken,
  handleRequestError,
}: UseGroupsAdminArgs) {
  const [groups, setGroups] = useState<Group[]>([]);
  const [isLoadingGroups, setIsLoadingGroups] = useState(false);
  const [groupsError, setGroupsError] = useState("");

  const [newGroupName, setNewGroupName] = useState("");
  const [newGroupDescription, setNewGroupDescription] = useState("");
  const [newGroupOrganizationId, setNewGroupOrganizationId] = useState("");
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

  async function loadGroups() {
    setIsLoadingGroups(true);
    setGroupsError("");

    try {
      const token = getStoredToken();
      const groupsData = await adminRequest<Group[]>(
        "/admin/groups",
        token,
        "No se pudo cargar la lista de grupos.",
      );

      setGroups(groupsData);
      setGroupEdits(buildGroupEditState(groupsData));

      if (
        membershipGroupId &&
        !groupsData.some((group) => String(group.id) === membershipGroupId)
      ) {
        setMembershipGroupId("");
      }
    } catch (loadError) {
      handleRequestError(
        loadError,
        setGroupsError,
        "No se pudo cargar la lista de grupos.",
      );
    } finally {
      setIsLoadingGroups(false);
    }
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

  async function handleCreateGroup(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setGroupFormError("");

    const organizationId = Number.parseInt(newGroupOrganizationId, 10);
    if (!Number.isInteger(organizationId)) {
      setGroupFormError("Selecciona una organización para el grupo.");
      return;
    }

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
            organization_id: organizationId,
          }),
        },
      );

      setNewGroupName("");
      setNewGroupDescription("");
      setNewGroupOrganizationId("");
      await loadGroups();
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
            organization_id: edit.organization_id,
          }),
        },
      );

      setGroupEditMessage("Grupo actualizado.");
      await loadGroups();
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
      await loadGroups();
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
      await loadGroups();
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
    groups,
    isLoadingGroups,
    groupsError,
    newGroupName,
    newGroupDescription,
    newGroupOrganizationId,
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
    setNewGroupName,
    setNewGroupDescription,
    setNewGroupOrganizationId,
    setMembershipUserId,
    setMembershipGroupId,
    loadGroups,
    updateGroupEdit,
    handleCreateGroup,
    handleUpdateGroup,
    handleDeleteGroup,
    updateMembership,
  };
}
