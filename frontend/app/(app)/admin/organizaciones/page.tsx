"use client";

import { useEffect, useState } from "react";
import { OrganizationsAdmin } from "../../../components/OrganizationsAdmin";
import {
  userHasPermission,
  type Municipality,
  type User,
} from "../../../components/types";
import { useOrganizationsAdmin } from "../../../lib/admin/useOrganizationsAdmin";
import {
  fetchAdminUsers,
  fetchMunicipalityOptions,
} from "../../../lib/fetchers";
import { useSession } from "../../../lib/session";

export default function AdminOrganizacionesPage() {
  const { user, getStoredToken, handleRequestError } = useSession();
  const organizationsAdmin = useOrganizationsAdmin({
    getStoredToken,
    handleRequestError,
  });
  const [municipalities, setMunicipalities] = useState<Municipality[]>([]);
  const [adminUsers, setAdminUsers] = useState<User[]>([]);
  // Fallos de las listas de referencia (municipios, usuarios); sin ellas los
  // selectores quedan vacíos, así que el error se muestra y "Actualizar"
  // permite reintentar la carga.
  const [referenceError, setReferenceError] = useState("");
  const canManageOrganizations = Boolean(
    user && userHasPermission(user, "organizations.manage"),
  );

  // Recarga el dominio y las listas de referencia; lo comparten el efecto de
  // montaje y el botón "Actualizar".
  function refreshAll() {
    setReferenceError("");
    void organizationsAdmin.loadOrganizations();

    fetchMunicipalityOptions()
      .then(setMunicipalities)
      .catch((referenceFetchError) => {
        handleRequestError(
          referenceFetchError,
          setReferenceError,
          "No se pudo cargar la lista de municipios.",
        );
      });

    fetchAdminUsers()
      .then(setAdminUsers)
      .catch((referenceFetchError) => {
        handleRequestError(
          referenceFetchError,
          setReferenceError,
          "No se pudo cargar la lista de usuarios.",
        );
      });
  }

  useEffect(() => {
    if (!canManageOrganizations) {
      return;
    }

    refreshAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id, user?.permissions]);

  if (!user || !canManageOrganizations) {
    return (
      <section className="panel">
        <p className="eyebrow">Administración</p>
        <h2>Acceso restringido</h2>
        <p className="muted">No tienes permisos de administración.</p>
      </section>
    );
  }

  return (
    <section className="panel admin-panel">
      <div className="panel-header">
        <div>
          <h2>Organizaciones</h2>
        </div>
        <button
          className="secondary-button"
          type="button"
          onClick={refreshAll}
          disabled={organizationsAdmin.isLoadingOrganizations}
        >
          {organizationsAdmin.isLoadingOrganizations
            ? "Cargando..."
            : "Actualizar"}
        </button>
      </div>

      {organizationsAdmin.organizationsError || referenceError ? (
        <p className="error-message">
          {organizationsAdmin.organizationsError || referenceError}
        </p>
      ) : null}

      <OrganizationsAdmin
        organizations={organizationsAdmin.organizations}
        municipalities={municipalities}
        adminUsers={adminUsers}
        isLoadingAdmin={organizationsAdmin.isLoadingOrganizations}
        newOrganizationName={organizationsAdmin.newOrganizationName}
        newOrganizationDescription={
          organizationsAdmin.newOrganizationDescription
        }
        newOrganizationMunicipalityId={
          organizationsAdmin.newOrganizationMunicipalityId
        }
        newOrganizationStatus={organizationsAdmin.newOrganizationStatus}
        organizationFormError={organizationsAdmin.organizationFormError}
        isCreatingOrganization={organizationsAdmin.isCreatingOrganization}
        organizationEdits={organizationsAdmin.organizationEdits}
        organizationEditError={organizationsAdmin.organizationEditError}
        organizationEditMessage={organizationsAdmin.organizationEditMessage}
        updatingOrganizationId={organizationsAdmin.updatingOrganizationId}
        organizationMembershipOrganizationId={
          organizationsAdmin.organizationMembershipOrganizationId
        }
        organizationMembershipUserId={
          organizationsAdmin.organizationMembershipUserId
        }
        organizationMembershipError={
          organizationsAdmin.organizationMembershipError
        }
        organizationMembershipMessage={
          organizationsAdmin.organizationMembershipMessage
        }
        isUpdatingOrganizationMembership={
          organizationsAdmin.isUpdatingOrganizationMembership
        }
        onNewOrganizationNameChange={
          organizationsAdmin.setNewOrganizationName
        }
        onNewOrganizationDescriptionChange={
          organizationsAdmin.setNewOrganizationDescription
        }
        onNewOrganizationMunicipalityIdChange={
          organizationsAdmin.setNewOrganizationMunicipalityId
        }
        onNewOrganizationStatusChange={
          organizationsAdmin.setNewOrganizationStatus
        }
        onCreateOrganization={organizationsAdmin.handleCreateOrganization}
        onUpdateOrganizationEdit={organizationsAdmin.updateOrganizationEdit}
        onUpdateOrganization={organizationsAdmin.handleUpdateOrganization}
        onOrganizationMembershipOrganizationIdChange={
          organizationsAdmin.setOrganizationMembershipOrganizationId
        }
        onOrganizationMembershipUserIdChange={
          organizationsAdmin.setOrganizationMembershipUserId
        }
        onUpdateOrganizationMembership={
          organizationsAdmin.updateOrganizationMembership
        }
      />
    </section>
  );
}
