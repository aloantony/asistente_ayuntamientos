"use client";

import { useEffect, useState } from "react";
import { GroupsAdmin } from "../../../components/GroupsAdmin";
import {
  formatUserOption,
  userHasPermission,
  type Group,
  type Organization,
  type User,
} from "../../../components/types";
import { useGroupsAdmin } from "../../../lib/admin/useGroupsAdmin";
import { fetchAdminUsers, fetchOrganizations } from "../../../lib/fetchers";
import { useSession } from "../../../lib/session";

export default function AdminGruposPage() {
  const { user, getStoredToken, handleRequestError } = useSession();
  const groupsAdmin = useGroupsAdmin({ getStoredToken, handleRequestError });
  const [organizations, setOrganizations] = useState<Organization[]>([]);
  const [adminUsers, setAdminUsers] = useState<User[]>([]);
  // Fallos de las listas de referencia (organizaciones, usuarios); sin ellas
  // los selectores quedan vacíos, así que el error se muestra y "Actualizar"
  // permite reintentar la carga.
  const [referenceError, setReferenceError] = useState("");
  const canManageGroups = Boolean(
    user && userHasPermission(user, "groups.manage"),
  );

  // Recarga el dominio y las listas de referencia; lo comparten el efecto de
  // montaje y el botón "Actualizar".
  function refreshAll() {
    setReferenceError("");
    void groupsAdmin.loadGroups();

    fetchOrganizations()
      .then(setOrganizations)
      .catch((referenceFetchError) => {
        handleRequestError(
          referenceFetchError,
          setReferenceError,
          "No se pudo cargar la lista de organizaciones.",
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
    if (!canManageGroups) {
      return;
    }

    refreshAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id, user?.permissions]);

  if (!user || !canManageGroups) {
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
          <h2>Grupos</h2>
        </div>
        <button
          className="secondary-button"
          type="button"
          onClick={refreshAll}
          disabled={groupsAdmin.isLoadingGroups}
        >
          {groupsAdmin.isLoadingGroups ? "Cargando..." : "Actualizar"}
        </button>
      </div>

      {groupsAdmin.groupsError || referenceError ? (
        <p className="error-message">
          {groupsAdmin.groupsError || referenceError}
        </p>
      ) : null}

      <GroupsAdmin
        groups={groupsAdmin.groups}
        organizations={organizations}
        isLoadingAdmin={groupsAdmin.isLoadingGroups}
        newGroupName={groupsAdmin.newGroupName}
        newGroupDescription={groupsAdmin.newGroupDescription}
        newGroupOrganizationId={groupsAdmin.newGroupOrganizationId}
        groupFormError={groupsAdmin.groupFormError}
        isCreatingGroup={groupsAdmin.isCreatingGroup}
        groupEdits={groupsAdmin.groupEdits}
        groupEditError={groupsAdmin.groupEditError}
        groupEditMessage={groupsAdmin.groupEditMessage}
        updatingGroupId={groupsAdmin.updatingGroupId}
        deletingGroupId={groupsAdmin.deletingGroupId}
        onNewGroupNameChange={groupsAdmin.setNewGroupName}
        onNewGroupDescriptionChange={groupsAdmin.setNewGroupDescription}
        onNewGroupOrganizationIdChange={groupsAdmin.setNewGroupOrganizationId}
        onCreateGroup={groupsAdmin.handleCreateGroup}
        onUpdateGroupEdit={groupsAdmin.updateGroupEdit}
        onUpdateGroup={groupsAdmin.handleUpdateGroup}
        onDeleteGroup={groupsAdmin.handleDeleteGroup}
      />

      <div className="admin-section">
        <h3>Pertenencia a grupos</h3>

        <div className="membership-controls">
          <label>
            Usuario
            <select
              onChange={(event) =>
                groupsAdmin.setMembershipUserId(event.target.value)
              }
              value={groupsAdmin.membershipUserId}
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
                groupsAdmin.setMembershipGroupId(event.target.value)
              }
              value={groupsAdmin.membershipGroupId}
            >
              <option value="">Selecciona un grupo</option>
              {groupsAdmin.groups.map((group: Group) => (
                <option key={group.id} value={group.id}>
                  {group.name} - {group.organization.name}
                </option>
              ))}
            </select>
          </label>
        </div>

        {groupsAdmin.membershipError ? (
          <p className="error-message">{groupsAdmin.membershipError}</p>
        ) : null}
        {groupsAdmin.membershipMessage ? (
          <p className="success-message">{groupsAdmin.membershipMessage}</p>
        ) : null}

        <div className="button-row">
          <button
            type="button"
            onClick={() => groupsAdmin.updateMembership("add")}
            disabled={groupsAdmin.isUpdatingMembership}
          >
            Añadir al grupo
          </button>
          <button
            className="secondary-button"
            type="button"
            onClick={() => groupsAdmin.updateMembership("remove")}
            disabled={groupsAdmin.isUpdatingMembership}
          >
            Quitar del grupo
          </button>
        </div>
      </div>
    </section>
  );
}
