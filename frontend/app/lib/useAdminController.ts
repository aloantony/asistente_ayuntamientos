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
  Municipality,
  MunicipalityEditState,
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
        municipality_id:
          organization.municipality_id === null
            ? ""
            : String(organization.municipality_id),
        status: organization.status,
      };
      return edits;
    },
    {},
  );
}

const EMPTY_MUNICIPALITY_FORM: MunicipalityEditState = {
  name: "",
  province: "",
  autonomous_community: "",
  country: "España",
  ine_code: "",
  population: "",
  surface_km2: "",
  density: "",
  postal_codes: "",
  municipality_type: "municipality",
  rural_urban_profile: "unknown",
  economic_profile: "",
  tourism_profile: "",
  geographic_notes: "",
  administrative_notes: "",
  status: "active",
};

function createEmptyMunicipalityForm(): MunicipalityEditState {
  return { ...EMPTY_MUNICIPALITY_FORM };
}

function buildMunicipalityEditState(municipalities: Municipality[]) {
  return municipalities.reduce<Record<number, MunicipalityEditState>>(
    (edits, municipality) => {
      edits[municipality.id] = {
        name: municipality.name,
        province: municipality.province,
        autonomous_community: municipality.autonomous_community,
        country: municipality.country,
        ine_code: municipality.ine_code ?? "",
        population:
          municipality.population === null ? "" : String(municipality.population),
        surface_km2:
          municipality.surface_km2 === null
            ? ""
            : String(municipality.surface_km2),
        density: municipality.density === null ? "" : String(municipality.density),
        postal_codes: municipality.postal_codes ?? "",
        municipality_type: municipality.municipality_type,
        rural_urban_profile: municipality.rural_urban_profile,
        economic_profile: municipality.economic_profile ?? "",
        tourism_profile: municipality.tourism_profile ?? "",
        geographic_notes: municipality.geographic_notes ?? "",
        administrative_notes: municipality.administrative_notes ?? "",
        status: municipality.status,
      };
      return edits;
    },
    {},
  );
}

function parseOptionalId(value: string) {
  if (!value) {
    return null;
  }

  const parsed = Number.parseInt(value, 10);
  return Number.isInteger(parsed) ? parsed : null;
}

function parseOptionalNumber(value: string) {
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }

  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : Number.NaN;
}

function buildMunicipalityPayload(edit: MunicipalityEditState) {
  const population = parseOptionalNumber(edit.population);
  if (
    population !== null &&
    (!Number.isInteger(population) || population < 0)
  ) {
    throw new Error("La población debe ser un número entero no negativo.");
  }

  const surfaceKm2 = parseOptionalNumber(edit.surface_km2);
  if (surfaceKm2 !== null && surfaceKm2 < 0) {
    throw new Error("La superficie debe ser un número no negativo.");
  }

  const density = parseOptionalNumber(edit.density);
  if (density !== null && density < 0) {
    throw new Error("La densidad debe ser un número no negativo.");
  }

  return {
    name: edit.name,
    province: edit.province,
    autonomous_community: edit.autonomous_community,
    country: edit.country,
    ine_code: edit.ine_code.trim() || null,
    population,
    surface_km2: surfaceKm2,
    density,
    postal_codes: edit.postal_codes.trim() || null,
    municipality_type: edit.municipality_type,
    rural_urban_profile: edit.rural_urban_profile,
    economic_profile: edit.economic_profile.trim() || null,
    tourism_profile: edit.tourism_profile.trim() || null,
    geographic_notes: edit.geographic_notes.trim() || null,
    administrative_notes: edit.administrative_notes.trim() || null,
    status: edit.status,
  };
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
  const [municipalities, setMunicipalities] = useState<Municipality[]>([]);
  const [groups, setGroups] = useState<Group[]>([]);
  const [isLoadingAdmin, setIsLoadingAdmin] = useState(false);
  const [adminError, setAdminError] = useState("");

  const [newOrganizationName, setNewOrganizationName] = useState("");
  const [newOrganizationDescription, setNewOrganizationDescription] =
    useState("");
  const [newOrganizationMunicipalityId, setNewOrganizationMunicipalityId] =
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

  const [municipalitySearchText, setMunicipalitySearchText] = useState("");
  const [municipalityProvinceFilter, setMunicipalityProvinceFilter] =
    useState("");
  const [
    municipalityAutonomousCommunityFilter,
    setMunicipalityAutonomousCommunityFilter,
  ] = useState("");
  const [municipalityStatusFilter, setMunicipalityStatusFilter] = useState("");
  const [municipalityIncludeArchived, setMunicipalityIncludeArchived] =
    useState(false);
  const [newMunicipality, setNewMunicipality] =
    useState<MunicipalityEditState>(createEmptyMunicipalityForm);
  const [municipalityFormError, setMunicipalityFormError] = useState("");
  const [isCreatingMunicipality, setIsCreatingMunicipality] = useState(false);
  const [municipalityEdits, setMunicipalityEdits] = useState<
    Record<number, MunicipalityEditState>
  >({});
  const [municipalityEditError, setMunicipalityEditError] = useState("");
  const [municipalityEditMessage, setMunicipalityEditMessage] = useState("");
  const [updatingMunicipalityId, setUpdatingMunicipalityId] = useState<
    number | null
  >(null);

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
    setMunicipalities([]);
    setGroups([]);
    setPermissions([]);
    setRoles([]);
    setUserEdits({});
    setOrganizationEdits({});
    setMunicipalityEdits({});
    setGroupEdits({});
    setRoleEdits({});
    setAdminError("");
    setNewOrganizationName("");
    setNewOrganizationDescription("");
    setNewOrganizationMunicipalityId("");
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
    setMunicipalitySearchText("");
    setMunicipalityProvinceFilter("");
    setMunicipalityAutonomousCommunityFilter("");
    setMunicipalityStatusFilter("");
    setMunicipalityIncludeArchived(false);
    setNewMunicipality(createEmptyMunicipalityForm());
    setMunicipalityFormError("");
    setIsCreatingMunicipality(false);
    setMunicipalityEditError("");
    setMunicipalityEditMessage("");
    setUpdatingMunicipalityId(null);
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
      const canLoadMunicipalities =
        canUsePermission("municipalities.view") ||
        canUsePermission("municipalities.manage");

      const [
        organizationsData,
        municipalitiesData,
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
        canLoadMunicipalities
          ? adminRequest<Municipality[]>(
              "/municipalities?include_archived=true",
              token,
              "No se pudo cargar la lista de municipios.",
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
      setMunicipalities(municipalitiesData);
      setAdminUsers(usersData);
      setGroups(groupsData);
      setPermissions(permissionsData);
      setRoles(rolesData);
      setOrganizationEdits(buildOrganizationEditState(organizationsData));
      setMunicipalityEdits(buildMunicipalityEditState(municipalitiesData));
      setUserEdits(buildUserEditState(usersData));
      setGroupEdits(buildGroupEditState(groupsData));
      setRoleEdits(buildRoleEditState(rolesData));

      if (
        newOrganizationMunicipalityId &&
        !municipalitiesData.some(
          (municipality) =>
            municipality.status === "active" &&
            String(municipality.id) === newOrganizationMunicipalityId,
        )
      ) {
        setNewOrganizationMunicipalityId("");
      }
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

  function updateNewMunicipality(updates: Partial<MunicipalityEditState>) {
    setNewMunicipality((currentMunicipality) => ({
      ...currentMunicipality,
      ...updates,
    }));
  }

  function updateMunicipalityEdit(
    municipalityId: number,
    updates: Partial<MunicipalityEditState>,
  ) {
    setMunicipalityEdits((currentEdits) => {
      const currentEdit = currentEdits[municipalityId];
      if (!currentEdit) {
        return currentEdits;
      }

      return {
        ...currentEdits,
        [municipalityId]: {
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
    const municipalityId = parseOptionalId(newOrganizationMunicipalityId);

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
            municipality_id: municipalityId,
            status: newOrganizationStatus,
          }),
        },
      );

      setNewOrganizationName("");
      setNewOrganizationDescription("");
      setNewOrganizationMunicipalityId("");
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
            municipality_id: parseOptionalId(edit.municipality_id),
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

  async function handleCreateMunicipality(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMunicipalityFormError("");
    setIsCreatingMunicipality(true);

    try {
      const token = getStoredToken();
      await adminRequest<Municipality>(
        "/municipalities",
        token,
        "No se pudo crear el municipio.",
        {
          method: "POST",
          body: JSON.stringify(buildMunicipalityPayload(newMunicipality)),
        },
      );

      setNewMunicipality(createEmptyMunicipalityForm());
      await loadAdminData();
    } catch (createError) {
      handleRequestError(
        createError,
        setMunicipalityFormError,
        "No se pudo crear el municipio.",
      );
    } finally {
      setIsCreatingMunicipality(false);
    }
  }

  async function handleUpdateMunicipality(municipalityId: number) {
    const edit = municipalityEdits[municipalityId];
    if (!edit) {
      setMunicipalityEditError("No se pudo encontrar el municipio para editar.");
      return;
    }

    setMunicipalityEditError("");
    setMunicipalityEditMessage("");
    setUpdatingMunicipalityId(municipalityId);

    try {
      const token = getStoredToken();
      await adminRequest<Municipality>(
        `/municipalities/${municipalityId}`,
        token,
        "No se pudo actualizar el municipio.",
        {
          method: "PATCH",
          body: JSON.stringify(buildMunicipalityPayload(edit)),
        },
      );

      setMunicipalityEditMessage("Municipio actualizado.");
      await loadAdminData();
    } catch (updateError) {
      handleRequestError(
        updateError,
        setMunicipalityEditError,
        "No se pudo actualizar el municipio.",
      );
    } finally {
      setUpdatingMunicipalityId(null);
    }
  }

  async function handleArchiveMunicipality(municipality: Municipality) {
    setMunicipalityEditError("");
    setMunicipalityEditMessage("");

    if (municipality.status === "archived") {
      setMunicipalityEditError("El municipio ya está archivado.");
      return;
    }

    const confirmed = window.confirm(
      `¿Archivar el municipio ${municipality.name}? No se eliminará definitivamente.`,
    );
    if (!confirmed) {
      return;
    }

    setUpdatingMunicipalityId(municipality.id);

    try {
      const token = getStoredToken();
      await adminRequest<Municipality>(
        `/municipalities/${municipality.id}`,
        token,
        "No se pudo archivar el municipio.",
        {
          method: "PATCH",
          body: JSON.stringify({ status: "archived" }),
        },
      );

      setMunicipalityEditMessage("Municipio archivado.");
      await loadAdminData();
    } catch (archiveError) {
      handleRequestError(
        archiveError,
        setMunicipalityEditError,
        "No se pudo archivar el municipio.",
      );
    } finally {
      setUpdatingMunicipalityId(null);
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
    municipalities,
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
    newOrganizationMunicipalityId,
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
    municipalitySearchText,
    municipalityProvinceFilter,
    municipalityAutonomousCommunityFilter,
    municipalityStatusFilter,
    municipalityIncludeArchived,
    newMunicipality,
    municipalityFormError,
    isCreatingMunicipality,
    municipalityEdits,
    municipalityEditError,
    municipalityEditMessage,
    updatingMunicipalityId,
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
    setNewOrganizationMunicipalityId,
    setNewOrganizationStatus,
    setOrganizationMembershipOrganizationId,
    setOrganizationMembershipUserId,
    setMunicipalitySearchText,
    setMunicipalityProvinceFilter,
    setMunicipalityAutonomousCommunityFilter,
    setMunicipalityStatusFilter,
    setMunicipalityIncludeArchived,
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
    updateNewMunicipality,
    updateMunicipalityEdit,
    updateGroupEdit,
    updateRoleEdit,
    handleCreateUser,
    handleUpdateUser,
    handleDeleteUser,
    handleCreateOrganization,
    handleUpdateOrganization,
    handleCreateMunicipality,
    handleUpdateMunicipality,
    handleArchiveMunicipality,
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
