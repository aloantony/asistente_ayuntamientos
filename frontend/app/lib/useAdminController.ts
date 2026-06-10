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
  GroupRoleResponse,
  MembershipAction,
  MembershipResponse,
  Organization,
  OrganizationEditState,
  OrganizationMembershipResponse,
  OrganizationStatus,
  Permission,
  PermissionBootstrapResponse,
  Role,
  RoleDeleteResponse,
  RoleEditState,
  RolePermissionResponse,
  User,
  UserDeleteResponse,
  UserEditState,
} from "../components/types";
import { userHasPermission } from "../components/types";
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
      organization_id: group.organization_id,
    };
    return edits;
  }, {});
}

function buildOrganizationEditState(organizations: Organization[]) {
  return organizations.reduce<Record<number, OrganizationEditState>>(
    (edits, organization) => {
      edits[organization.id] = {
        name: organization.name,
        description: organization.description ?? "",
        status: organization.status,
      };
      return edits;
    },
    {},
  );
}

function buildRoleEditState(roles: Role[]) {
  return roles.reduce<Record<number, RoleEditState>>((edits, role) => {
    edits[role.id] = {
      name: role.name,
      description: role.description ?? "",
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
  const [organizations, setOrganizations] = useState<Organization[]>([]);
  const [groups, setGroups] = useState<Group[]>([]);
  const [isLoadingAdmin, setIsLoadingAdmin] = useState(false);
  const [adminError, setAdminError] = useState("");

  const [newOrganizationName, setNewOrganizationName] = useState("");
  const [newOrganizationDescription, setNewOrganizationDescription] =
    useState("");
  const [newOrganizationStatus, setNewOrganizationStatus] =
    useState<OrganizationStatus>("active");
  const [organizationFormError, setOrganizationFormError] = useState("");
  const [isCreatingOrganization, setIsCreatingOrganization] = useState(false);
  const [organizationEdits, setOrganizationEdits] = useState<
    Record<number, OrganizationEditState>
  >({});
  const [organizationEditError, setOrganizationEditError] = useState("");
  const [organizationEditMessage, setOrganizationEditMessage] = useState("");
  const [updatingOrganizationId, setUpdatingOrganizationId] = useState<
    number | null
  >(null);
  const [organizationMembershipOrganizationId, setOrganizationMembershipOrganizationId] =
    useState("");
  const [organizationMembershipUserId, setOrganizationMembershipUserId] =
    useState("");
  const [organizationMembershipError, setOrganizationMembershipError] =
    useState("");
  const [organizationMembershipMessage, setOrganizationMembershipMessage] =
    useState("");
  const [isUpdatingOrganizationMembership, setIsUpdatingOrganizationMembership] =
    useState(false);

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

  const [permissions, setPermissions] = useState<Permission[]>([]);
  const [roles, setRoles] = useState<Role[]>([]);
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

  function clearAdminState() {
    setAdminUsers([]);
    setOrganizations([]);
    setGroups([]);
    setPermissions([]);
    setRoles([]);
    setUserEdits({});
    setOrganizationEdits({});
    setGroupEdits({});
    setRoleEdits({});
    setAdminError("");
    setNewOrganizationName("");
    setNewOrganizationDescription("");
    setNewOrganizationStatus("active");
    setOrganizationFormError("");
    setOrganizationEditError("");
    setOrganizationEditMessage("");
    setUpdatingOrganizationId(null);
    setOrganizationMembershipOrganizationId("");
    setOrganizationMembershipUserId("");
    setOrganizationMembershipError("");
    setOrganizationMembershipMessage("");
    setIsUpdatingOrganizationMembership(false);
    setUserFormError("");
    setUserEditError("");
    setUserEditMessage("");
    setDeletingUserId(null);
    setNewGroupOrganizationId("");
    setGroupFormError("");
    setGroupEditError("");
    setGroupEditMessage("");
    setDeletingGroupId(null);
    setMembershipError("");
    setMembershipMessage("");
    setNewRoleName("");
    setNewRoleDescription("");
    setRoleFormError("");
    setRoleEditError("");
    setRoleEditMessage("");
    setUpdatingRoleId(null);
    setDeletingRoleId(null);
    setRolePermissionRoleId("");
    setRolePermissionPermissionId("");
    setRolePermissionError("");
    setRolePermissionMessage("");
    setGroupRoleGroupId("");
    setGroupRoleRoleId("");
    setGroupRoleError("");
    setGroupRoleMessage("");
    setIsUpdatingRolePermission(false);
    setIsUpdatingGroupRole(false);
    setBootstrapPermissionsError("");
    setBootstrapPermissionsMessage("");
    setIsBootstrappingPermissions(false);
  }

  function canUsePermission(permissionCode: string) {
    return Boolean(user && userHasPermission(user, permissionCode));
  }

  async function loadAdminData() {
    setIsLoadingAdmin(true);
    setAdminError("");

    try {
      const token = getStoredToken();
      const canLoadOrganizations = Boolean(user);
      const canLoadUsers =
        canUsePermission("users.manage") ||
        canUsePermission("groups.manage") ||
        canUsePermission("organizations.manage") ||
        canUsePermission("projects.manage_members");
      const canLoadGroups =
        canUsePermission("groups.manage") ||
        canUsePermission("roles.manage") ||
        canUsePermission("projects.manage_members");
      const canLoadRbac = canUsePermission("roles.manage");

      const [
        organizationsData,
        usersData,
        groupsData,
        permissionsData,
        rolesData,
      ] = await Promise.all([
        canLoadOrganizations
          ? adminRequest<Organization[]>(
              "/organizations",
              token,
              "No se pudo cargar la lista de organizaciones.",
            )
          : Promise.resolve([]),
        canLoadUsers
          ? adminRequest<User[]>(
              "/admin/users",
              token,
              "No se pudo cargar la lista de usuarios.",
            )
          : Promise.resolve([]),
        canLoadGroups
          ? adminRequest<Group[]>(
              "/admin/groups",
              token,
              "No se pudo cargar la lista de grupos.",
            )
          : Promise.resolve([]),
        canLoadRbac
          ? adminRequest<Permission[]>(
              "/admin/permissions",
              token,
              "No se pudo cargar la lista de permisos.",
            )
          : Promise.resolve([]),
        canLoadRbac
          ? adminRequest<Role[]>(
              "/admin/roles",
              token,
              "No se pudo cargar la lista de roles.",
            )
          : Promise.resolve([]),
      ]);

      setOrganizations(organizationsData);
      setAdminUsers(usersData);
      setGroups(groupsData);
      setPermissions(permissionsData);
      setRoles(rolesData);
      setOrganizationEdits(buildOrganizationEditState(organizationsData));
      setUserEdits(buildUserEditState(usersData));
      setGroupEdits(buildGroupEditState(groupsData));
      setRoleEdits(buildRoleEditState(rolesData));

      if (
        newGroupOrganizationId &&
        !organizationsData.some(
          (organization) => String(organization.id) === newGroupOrganizationId,
        )
      ) {
        setNewGroupOrganizationId("");
      }
      if (
        organizationMembershipOrganizationId &&
        !organizationsData.some(
          (organization) =>
            String(organization.id) === organizationMembershipOrganizationId,
        )
      ) {
        setOrganizationMembershipOrganizationId("");
      }
      if (
        organizationMembershipUserId &&
        !usersData.some(
          (adminUser) => String(adminUser.id) === organizationMembershipUserId,
        )
      ) {
        setOrganizationMembershipUserId("");
      }

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
        groupRoleGroupId &&
        !groupsData.some((group) => String(group.id) === groupRoleGroupId)
      ) {
        setGroupRoleGroupId("");
      }
      if (
        groupRoleRoleId &&
        !rolesData.some((role) => String(role.id) === groupRoleRoleId)
      ) {
        setGroupRoleRoleId("");
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

  function updateOrganizationEdit(
    organizationId: number,
    updates: Partial<OrganizationEditState>,
  ) {
    setOrganizationEdits((currentEdits) => {
      const currentEdit = currentEdits[organizationId];
      if (!currentEdit) {
        return currentEdits;
      }

      return {
        ...currentEdits,
        [organizationId]: {
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

  async function handleCreateOrganization(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setOrganizationFormError("");
    setIsCreatingOrganization(true);

    try {
      const token = getStoredToken();
      await adminRequest<Organization>(
        "/organizations",
        token,
        "No se pudo crear la organización.",
        {
          method: "POST",
          body: JSON.stringify({
            name: newOrganizationName,
            description: newOrganizationDescription.trim() || null,
            status: newOrganizationStatus,
          }),
        },
      );

      setNewOrganizationName("");
      setNewOrganizationDescription("");
      setNewOrganizationStatus("active");
      await loadAdminData();
    } catch (createError) {
      handleRequestError(
        createError,
        setOrganizationFormError,
        "No se pudo crear la organización.",
      );
    } finally {
      setIsCreatingOrganization(false);
    }
  }

  async function handleUpdateOrganization(organizationId: number) {
    const edit = organizationEdits[organizationId];
    if (!edit) {
      setOrganizationEditError(
        "No se pudo encontrar la organización para editar.",
      );
      return;
    }

    const name = edit.name.trim();
    if (!name) {
      setOrganizationEditError("El nombre de la organización no puede estar vacío.");
      setOrganizationEditMessage("");
      return;
    }

    setOrganizationEditError("");
    setOrganizationEditMessage("");
    setUpdatingOrganizationId(organizationId);

    try {
      const token = getStoredToken();
      await adminRequest<Organization>(
        `/organizations/${organizationId}`,
        token,
        "No se pudo actualizar la organización.",
        {
          method: "PATCH",
          body: JSON.stringify({
            name,
            description: edit.description.trim() || null,
            status: edit.status,
          }),
        },
      );

      setOrganizationEditMessage("Organización actualizada.");
      await loadAdminData();
      await loadProjects();
    } catch (updateError) {
      handleRequestError(
        updateError,
        setOrganizationEditError,
        "No se pudo actualizar la organización.",
      );
    } finally {
      setUpdatingOrganizationId(null);
    }
  }

  async function updateOrganizationMembership(action: MembershipAction) {
    setOrganizationMembershipError("");
    setOrganizationMembershipMessage("");

    const organizationId = Number.parseInt(
      organizationMembershipOrganizationId,
      10,
    );
    const userId = Number.parseInt(organizationMembershipUserId, 10);

    if (!Number.isInteger(organizationId) || !Number.isInteger(userId)) {
      setOrganizationMembershipError("Selecciona una organización y un usuario.");
      return;
    }

    setIsUpdatingOrganizationMembership(true);

    try {
      const token = getStoredToken();
      await adminRequest<OrganizationMembershipResponse>(
        `/organizations/${organizationId}/users/${userId}`,
        token,
        "No se pudo actualizar el usuario de la organización.",
        {
          method: action === "add" ? "POST" : "DELETE",
        },
      );

      setOrganizationMembershipMessage(
        action === "add"
          ? "Usuario añadido a la organización."
          : "Usuario quitado de la organización.",
      );
      await loadAdminData();
      await loadProjects();
    } catch (membershipUpdateError) {
      handleRequestError(
        membershipUpdateError,
        setOrganizationMembershipError,
        "No se pudo actualizar el usuario de la organización.",
      );
    } finally {
      setIsUpdatingOrganizationMembership(false);
    }
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
            organization_id: edit.organization_id,
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
      if (groupRoleGroupId === String(group.id)) {
        setGroupRoleGroupId("");
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
      await loadAdminData();
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
      await loadAdminData();
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
      await loadAdminData();
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
      await loadAdminData();
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
      await loadAdminData();
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
      await loadAdminData();
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
    adminUsers,
    organizations,
    groups,
    permissions,
    roles,
    isLoadingAdmin,
    adminError,
    newUserEmail,
    newUserPassword,
    newUserFullName,
    newUserIsActive,
    newUserIsSuperuser,
    newOrganizationName,
    newOrganizationDescription,
    newOrganizationStatus,
    organizationFormError,
    isCreatingOrganization,
    organizationEdits,
    organizationEditError,
    organizationEditMessage,
    updatingOrganizationId,
    organizationMembershipOrganizationId,
    organizationMembershipUserId,
    organizationMembershipError,
    organizationMembershipMessage,
    isUpdatingOrganizationMembership,
    userFormError,
    isCreatingUser,
    userEdits,
    userEditError,
    userEditMessage,
    updatingUserId,
    deletingUserId,
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
    setNewUserEmail,
    setNewUserPassword,
    setNewUserFullName,
    setNewUserIsActive,
    setNewUserIsSuperuser,
    setNewOrganizationName,
    setNewOrganizationDescription,
    setNewOrganizationStatus,
    setOrganizationMembershipOrganizationId,
    setOrganizationMembershipUserId,
    setNewGroupName,
    setNewGroupDescription,
    setNewGroupOrganizationId,
    setMembershipUserId,
    setMembershipGroupId,
    setNewRoleName,
    setNewRoleDescription,
    setRolePermissionRoleId,
    setRolePermissionPermissionId,
    setGroupRoleGroupId,
    setGroupRoleRoleId,
    clearAdminState,
    loadAdminData,
    updateUserEdit,
    updateOrganizationEdit,
    updateGroupEdit,
    updateRoleEdit,
    handleCreateUser,
    handleUpdateUser,
    handleDeleteUser,
    handleCreateOrganization,
    handleUpdateOrganization,
    updateOrganizationMembership,
    handleCreateGroup,
    handleUpdateGroup,
    handleDeleteGroup,
    updateMembership,
    handleBootstrapPermissions,
    handleCreateRole,
    handleUpdateRole,
    handleDeleteRole,
    updateRolePermission,
    updateGroupRole,
  };
}
