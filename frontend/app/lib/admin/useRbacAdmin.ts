"use client";

import { type FormEvent, useState } from "react";
import type {
  GroupRoleResponse,
  MembershipAction,
  Permission,
  PermissionBootstrapResponse,
  Role,
  RoleDeleteResponse,
  RoleEditState,
  RolePermissionResponse,
} from "../../components/types";
import { adminRequest } from "../api";

type RequestErrorHandler = (
  requestError: unknown,
  setMessage: (message: string) => void,
  fallback: string,
) => void;

type UseRbacAdminArgs = {
  getStoredToken: () => string;
  handleRequestError: RequestErrorHandler;
};

function buildRoleEditState(roles: Role[]) {
  return roles.reduce<Record<number, RoleEditState>>((edits, role) => {
    edits[role.id] = {
      name: role.name,
      description: role.description ?? "",
    };
    return edits;
  }, {});
}

export function useRbacAdmin({
  getStoredToken,
  handleRequestError,
}: UseRbacAdminArgs) {
  const [permissions, setPermissions] = useState<Permission[]>([]);
  const [roles, setRoles] = useState<Role[]>([]);
  const [isLoadingRbac, setIsLoadingRbac] = useState(false);
  const [rbacError, setRbacError] = useState("");

  const [newRoleName, setNewRoleName] = useState("");
  const [newRoleDescription, setNewRoleDescription] = useState("");
  const [roleFormError, setRoleFormError] = useState("");
  const [isCreatingRole, setIsCreatingRole] = useState(false);
  const [roleEdits, setRoleEdits] = useState<Record<number, RoleEditState>>(
    {},
  );
  const [roleEditError, setRoleEditError] = useState("");
  const [roleEditMessage, setRoleEditMessage] = useState("");
  const [updatingRoleId, setUpdatingRoleId] = useState<number | null>(null);
  const [deletingRoleId, setDeletingRoleId] = useState<number | null>(null);
  const [rolePermissionRoleId, setRolePermissionRoleId] = useState("");
  const [rolePermissionPermissionId, setRolePermissionPermissionId] =
    useState("");
  const [rolePermissionError, setRolePermissionError] = useState("");
  const [rolePermissionMessage, setRolePermissionMessage] = useState("");
  const [isUpdatingRolePermission, setIsUpdatingRolePermission] =
    useState(false);
  const [groupRoleGroupId, setGroupRoleGroupId] = useState("");
  const [groupRoleRoleId, setGroupRoleRoleId] = useState("");
  const [groupRoleError, setGroupRoleError] = useState("");
  const [groupRoleMessage, setGroupRoleMessage] = useState("");
  const [isUpdatingGroupRole, setIsUpdatingGroupRole] = useState(false);
  const [bootstrapPermissionsError, setBootstrapPermissionsError] =
    useState("");
  const [bootstrapPermissionsMessage, setBootstrapPermissionsMessage] =
    useState("");
  const [isBootstrappingPermissions, setIsBootstrappingPermissions] =
    useState(false);

  async function loadRbac() {
    setIsLoadingRbac(true);
    setRbacError("");

    try {
      const token = getStoredToken();
      const [permissionsData, rolesData] = await Promise.all([
        adminRequest<Permission[]>(
          "/admin/permissions",
          token,
          "No se pudo cargar la lista de permisos.",
        ),
        adminRequest<Role[]>(
          "/admin/roles",
          token,
          "No se pudo cargar la lista de roles.",
        ),
      ]);

      setPermissions(permissionsData);
      setRoles(rolesData);
      setRoleEdits(buildRoleEditState(rolesData));

      if (
        rolePermissionRoleId &&
        !rolesData.some((role) => String(role.id) === rolePermissionRoleId)
      ) {
        setRolePermissionRoleId("");
      }
      if (
        rolePermissionPermissionId &&
        !permissionsData.some(
          (permission) => String(permission.id) === rolePermissionPermissionId,
        )
      ) {
        setRolePermissionPermissionId("");
      }
      if (
        groupRoleRoleId &&
        !rolesData.some((role) => String(role.id) === groupRoleRoleId)
      ) {
        setGroupRoleRoleId("");
      }
    } catch (loadError) {
      handleRequestError(
        loadError,
        setRbacError,
        "No se pudo cargar la información de roles y permisos.",
      );
    } finally {
      setIsLoadingRbac(false);
    }
  }

  function updateRoleEdit(roleId: number, updates: Partial<RoleEditState>) {
    setRoleEdits((currentEdits) => {
      const currentEdit = currentEdits[roleId];
      if (!currentEdit) {
        return currentEdits;
      }

      return {
        ...currentEdits,
        [roleId]: {
          ...currentEdit,
          ...updates,
        },
      };
    });
  }

  async function handleBootstrapPermissions() {
    setBootstrapPermissionsError("");
    setBootstrapPermissionsMessage("");
    setIsBootstrappingPermissions(true);

    try {
      const token = getStoredToken();
      const response = await adminRequest<PermissionBootstrapResponse>(
        "/admin/permissions/bootstrap",
        token,
        "No se pudieron inicializar los permisos base.",
        {
          method: "POST",
        },
      );

      setPermissions(response.permissions);
      setBootstrapPermissionsMessage(
        response.created_codes.length > 0
          ? `Permisos creados: ${response.created_codes.join(", ")}.`
          : "Los permisos base ya estaban inicializados.",
      );
      await loadRbac();
    } catch (bootstrapError) {
      handleRequestError(
        bootstrapError,
        setBootstrapPermissionsError,
        "No se pudieron inicializar los permisos base.",
      );
    } finally {
      setIsBootstrappingPermissions(false);
    }
  }

  async function handleCreateRole(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setRoleFormError("");
    setIsCreatingRole(true);

    try {
      const token = getStoredToken();
      await adminRequest<Role>(
        "/admin/roles",
        token,
        "No se pudo crear el rol.",
        {
          method: "POST",
          body: JSON.stringify({
            name: newRoleName,
            description: newRoleDescription.trim() || null,
          }),
        },
      );

      setNewRoleName("");
      setNewRoleDescription("");
      await loadRbac();
    } catch (createError) {
      handleRequestError(
        createError,
        setRoleFormError,
        "No se pudo crear el rol.",
      );
    } finally {
      setIsCreatingRole(false);
    }
  }

  async function handleUpdateRole(roleId: number) {
    const edit = roleEdits[roleId];
    if (!edit) {
      setRoleEditError("No se pudo encontrar el rol para editar.");
      return;
    }

    const name = edit.name.trim();
    if (!name) {
      setRoleEditError("El nombre del rol no puede estar vacío.");
      setRoleEditMessage("");
      return;
    }

    setRoleEditError("");
    setRoleEditMessage("");
    setUpdatingRoleId(roleId);

    try {
      const token = getStoredToken();
      await adminRequest<Role>(
        `/admin/roles/${roleId}`,
        token,
        "No se pudo actualizar el rol.",
        {
          method: "PATCH",
          body: JSON.stringify({
            name,
            description: edit.description.trim() || null,
          }),
        },
      );

      setRoleEditMessage("Rol actualizado.");
      await loadRbac();
    } catch (updateError) {
      handleRequestError(
        updateError,
        setRoleEditError,
        "No se pudo actualizar el rol.",
      );
    } finally {
      setUpdatingRoleId(null);
    }
  }

  async function handleDeleteRole(role: Role) {
    setRoleEditError("");
    setRoleEditMessage("");

    const confirmed = window.confirm(
      `¿Eliminar el rol ${role.name}? Esta acción no se puede deshacer.`,
    );
    if (!confirmed) {
      return;
    }

    setDeletingRoleId(role.id);

    try {
      const token = getStoredToken();
      await adminRequest<RoleDeleteResponse>(
        `/admin/roles/${role.id}`,
        token,
        "No se pudo eliminar el rol.",
        {
          method: "DELETE",
        },
      );

      if (rolePermissionRoleId === String(role.id)) {
        setRolePermissionRoleId("");
      }
      if (groupRoleRoleId === String(role.id)) {
        setGroupRoleRoleId("");
      }

      setRoleEditMessage("Rol eliminado.");
      await loadRbac();
    } catch (deleteError) {
      handleRequestError(
        deleteError,
        setRoleEditError,
        "No se pudo eliminar el rol.",
      );
    } finally {
      setDeletingRoleId(null);
    }
  }

  async function updateRolePermission(action: MembershipAction) {
    setRolePermissionError("");
    setRolePermissionMessage("");

    const roleId = Number.parseInt(rolePermissionRoleId, 10);
    const permissionId = Number.parseInt(rolePermissionPermissionId, 10);

    if (!Number.isInteger(roleId) || !Number.isInteger(permissionId)) {
      setRolePermissionError("Selecciona un rol y un permiso.");
      return;
    }

    setIsUpdatingRolePermission(true);

    try {
      const token = getStoredToken();
      await adminRequest<RolePermissionResponse>(
        `/admin/roles/${roleId}/permissions/${permissionId}`,
        token,
        "No se pudo actualizar el permiso del rol.",
        {
          method: action === "add" ? "POST" : "DELETE",
        },
      );

      setRolePermissionMessage(
        action === "add"
          ? "Permiso asignado al rol."
          : "Permiso eliminado del rol.",
      );
      await loadRbac();
    } catch (permissionUpdateError) {
      handleRequestError(
        permissionUpdateError,
        setRolePermissionError,
        "No se pudo actualizar el permiso del rol.",
      );
    } finally {
      setIsUpdatingRolePermission(false);
    }
  }

  async function updateGroupRole(action: MembershipAction) {
    setGroupRoleError("");
    setGroupRoleMessage("");

    const groupId = Number.parseInt(groupRoleGroupId, 10);
    const roleId = Number.parseInt(groupRoleRoleId, 10);

    if (!Number.isInteger(groupId) || !Number.isInteger(roleId)) {
      setGroupRoleError("Selecciona un grupo y un rol.");
      return;
    }

    setIsUpdatingGroupRole(true);

    try {
      const token = getStoredToken();
      await adminRequest<GroupRoleResponse>(
        `/admin/groups/${groupId}/roles/${roleId}`,
        token,
        "No se pudo actualizar el rol del grupo.",
        {
          method: action === "add" ? "POST" : "DELETE",
        },
      );

      setGroupRoleMessage(
        action === "add"
          ? "Rol asignado al grupo."
          : "Rol eliminado del grupo.",
      );
      await loadRbac();
    } catch (groupRoleUpdateError) {
      handleRequestError(
        groupRoleUpdateError,
        setGroupRoleError,
        "No se pudo actualizar el rol del grupo.",
      );
    } finally {
      setIsUpdatingGroupRole(false);
    }
  }

  return {
    permissions,
    roles,
    isLoadingRbac,
    rbacError,
    newRoleName,
    newRoleDescription,
    roleFormError,
    isCreatingRole,
    roleEdits,
    roleEditError,
    roleEditMessage,
    updatingRoleId,
    deletingRoleId,
    rolePermissionRoleId,
    rolePermissionPermissionId,
    rolePermissionError,
    rolePermissionMessage,
    isUpdatingRolePermission,
    groupRoleGroupId,
    groupRoleRoleId,
    groupRoleError,
    groupRoleMessage,
    isUpdatingGroupRole,
    bootstrapPermissionsError,
    bootstrapPermissionsMessage,
    isBootstrappingPermissions,
    setNewRoleName,
    setNewRoleDescription,
    setRolePermissionRoleId,
    setRolePermissionPermissionId,
    setGroupRoleGroupId,
    setGroupRoleRoleId,
    loadRbac,
    updateRoleEdit,
    handleBootstrapPermissions,
    handleCreateRole,
    handleUpdateRole,
    handleDeleteRole,
    updateRolePermission,
    updateGroupRole,
  };
}
