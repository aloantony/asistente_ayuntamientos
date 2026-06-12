"use client";

import { useEffect, useState } from "react";
import { RbacAdmin } from "../../../components/RbacAdmin";
import { userHasPermission, type Group } from "../../../components/types";
import { useRbacAdmin } from "../../../lib/admin/useRbacAdmin";
import { fetchAdminGroups } from "../../../lib/fetchers";
import { useSession } from "../../../lib/session";

export default function AdminRolesPage() {
  const { user, getStoredToken, handleRequestError } = useSession();
  const rbacAdmin = useRbacAdmin({ getStoredToken, handleRequestError });
  const [groups, setGroups] = useState<Group[]>([]);
  // Fallo de la lista de referencia (grupos); sin ella el selector queda
  // vacío, así que el error se muestra y "Actualizar" permite reintentar.
  const [referenceError, setReferenceError] = useState("");
  const canManageRoles = Boolean(
    user && userHasPermission(user, "roles.manage"),
  );

  // Recarga el dominio y la lista de referencia; lo comparten el efecto de
  // montaje y el botón "Actualizar".
  function refreshAll() {
    setReferenceError("");
    void rbacAdmin.loadRbac();

    fetchAdminGroups()
      .then(setGroups)
      .catch((referenceFetchError) => {
        handleRequestError(
          referenceFetchError,
          setReferenceError,
          "No se pudo cargar la lista de grupos.",
        );
      });
  }

  useEffect(() => {
    if (!canManageRoles) {
      return;
    }

    refreshAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id, user?.permissions]);

  if (!user || !canManageRoles) {
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
          <h2>Roles y permisos</h2>
        </div>
        <button
          className="secondary-button"
          type="button"
          onClick={refreshAll}
          disabled={rbacAdmin.isLoadingRbac}
        >
          {rbacAdmin.isLoadingRbac ? "Cargando..." : "Actualizar"}
        </button>
      </div>

      {rbacAdmin.rbacError || referenceError ? (
        <p className="error-message">{rbacAdmin.rbacError || referenceError}</p>
      ) : null}

      <RbacAdmin
        permissions={rbacAdmin.permissions}
        roles={rbacAdmin.roles}
        groups={groups}
        isLoadingAdmin={rbacAdmin.isLoadingRbac}
        newRoleName={rbacAdmin.newRoleName}
        newRoleDescription={rbacAdmin.newRoleDescription}
        roleFormError={rbacAdmin.roleFormError}
        isCreatingRole={rbacAdmin.isCreatingRole}
        roleEdits={rbacAdmin.roleEdits}
        roleEditError={rbacAdmin.roleEditError}
        roleEditMessage={rbacAdmin.roleEditMessage}
        updatingRoleId={rbacAdmin.updatingRoleId}
        deletingRoleId={rbacAdmin.deletingRoleId}
        rolePermissionRoleId={rbacAdmin.rolePermissionRoleId}
        rolePermissionPermissionId={rbacAdmin.rolePermissionPermissionId}
        rolePermissionError={rbacAdmin.rolePermissionError}
        rolePermissionMessage={rbacAdmin.rolePermissionMessage}
        isUpdatingRolePermission={rbacAdmin.isUpdatingRolePermission}
        groupRoleGroupId={rbacAdmin.groupRoleGroupId}
        groupRoleRoleId={rbacAdmin.groupRoleRoleId}
        groupRoleError={rbacAdmin.groupRoleError}
        groupRoleMessage={rbacAdmin.groupRoleMessage}
        isUpdatingGroupRole={rbacAdmin.isUpdatingGroupRole}
        bootstrapPermissionsError={rbacAdmin.bootstrapPermissionsError}
        bootstrapPermissionsMessage={rbacAdmin.bootstrapPermissionsMessage}
        isBootstrappingPermissions={rbacAdmin.isBootstrappingPermissions}
        onBootstrapPermissions={rbacAdmin.handleBootstrapPermissions}
        onNewRoleNameChange={rbacAdmin.setNewRoleName}
        onNewRoleDescriptionChange={rbacAdmin.setNewRoleDescription}
        onCreateRole={rbacAdmin.handleCreateRole}
        onUpdateRoleEdit={rbacAdmin.updateRoleEdit}
        onUpdateRole={rbacAdmin.handleUpdateRole}
        onDeleteRole={rbacAdmin.handleDeleteRole}
        onRolePermissionRoleIdChange={rbacAdmin.setRolePermissionRoleId}
        onRolePermissionPermissionIdChange={
          rbacAdmin.setRolePermissionPermissionId
        }
        onUpdateRolePermission={rbacAdmin.updateRolePermission}
        onGroupRoleGroupIdChange={rbacAdmin.setGroupRoleGroupId}
        onGroupRoleRoleIdChange={rbacAdmin.setGroupRoleRoleId}
        onUpdateGroupRole={rbacAdmin.updateGroupRole}
      />
    </section>
  );
}
