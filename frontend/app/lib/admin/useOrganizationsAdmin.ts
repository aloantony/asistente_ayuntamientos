"use client";

import { type FormEvent, useState } from "react";
import type {
  MembershipAction,
  Organization,
  OrganizationEditState,
  OrganizationMembershipResponse,
  OrganizationStatus,
} from "../../components/types";
import { adminRequest } from "../api";

type RequestErrorHandler = (
  requestError: unknown,
  setMessage: (message: string) => void,
  fallback: string,
) => void;

type UseOrganizationsAdminArgs = {
  getStoredToken: () => string;
  handleRequestError: RequestErrorHandler;
};

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

function parseOptionalId(value: string) {
  if (!value) {
    return null;
  }

  const parsed = Number.parseInt(value, 10);
  return Number.isInteger(parsed) ? parsed : null;
}

export function useOrganizationsAdmin({
  getStoredToken,
  handleRequestError,
}: UseOrganizationsAdminArgs) {
  const [organizations, setOrganizations] = useState<Organization[]>([]);
  const [isLoadingOrganizations, setIsLoadingOrganizations] = useState(false);
  const [organizationsError, setOrganizationsError] = useState("");

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

  async function loadOrganizations() {
    setIsLoadingOrganizations(true);
    setOrganizationsError("");

    try {
      const token = getStoredToken();
      const organizationsData = await adminRequest<Organization[]>(
        "/organizations",
        token,
        "No se pudo cargar la lista de organizaciones.",
      );

      setOrganizations(organizationsData);
      setOrganizationEdits(buildOrganizationEditState(organizationsData));

      if (
        organizationMembershipOrganizationId &&
        !organizationsData.some(
          (organization) =>
            String(organization.id) === organizationMembershipOrganizationId,
        )
      ) {
        setOrganizationMembershipOrganizationId("");
      }
    } catch (loadError) {
      handleRequestError(
        loadError,
        setOrganizationsError,
        "No se pudo cargar la lista de organizaciones.",
      );
    } finally {
      setIsLoadingOrganizations(false);
    }
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
      await loadOrganizations();
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
      await loadOrganizations();
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
      await loadOrganizations();
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

  return {
    organizations,
    isLoadingOrganizations,
    organizationsError,
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
    setNewOrganizationName,
    setNewOrganizationDescription,
    setNewOrganizationMunicipalityId,
    setNewOrganizationStatus,
    setOrganizationMembershipOrganizationId,
    setOrganizationMembershipUserId,
    loadOrganizations,
    updateOrganizationEdit,
    handleCreateOrganization,
    handleUpdateOrganization,
    updateOrganizationMembership,
  };
}
