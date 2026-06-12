"use client";

import { useEffect } from "react";
import { UsersAdmin } from "../../../components/UsersAdmin";
import { userHasPermission } from "../../../components/types";
import { useUsersAdmin } from "../../../lib/admin/useUsersAdmin";
import { useSession } from "../../../lib/session";

export default function AdminUsuariosPage() {
  const { user, setUser, getStoredToken, handleRequestError } = useSession();
  const usersAdmin = useUsersAdmin({
    getStoredToken,
    handleRequestError,
    user,
    setUser,
  });
  const canManageUsers = Boolean(
    user && userHasPermission(user, "users.manage"),
  );

  useEffect(() => {
    if (!canManageUsers) {
      return;
    }

    void usersAdmin.loadUsers();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id, user?.permissions]);

  if (!user || !canManageUsers) {
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
          <h2>Usuarios</h2>
        </div>
        <button
          className="secondary-button"
          type="button"
          onClick={() => void usersAdmin.loadUsers()}
          disabled={usersAdmin.isLoadingUsers}
        >
          {usersAdmin.isLoadingUsers ? "Cargando..." : "Actualizar"}
        </button>
      </div>

      {usersAdmin.usersError ? (
        <p className="error-message">{usersAdmin.usersError}</p>
      ) : null}

      <UsersAdmin
        currentUser={user}
        adminUsers={usersAdmin.adminUsers}
        isLoadingAdmin={usersAdmin.isLoadingUsers}
        newUserEmail={usersAdmin.newUserEmail}
        newUserPassword={usersAdmin.newUserPassword}
        newUserFullName={usersAdmin.newUserFullName}
        newUserIsActive={usersAdmin.newUserIsActive}
        newUserIsSuperuser={usersAdmin.newUserIsSuperuser}
        userFormError={usersAdmin.userFormError}
        isCreatingUser={usersAdmin.isCreatingUser}
        userEdits={usersAdmin.userEdits}
        userEditError={usersAdmin.userEditError}
        userEditMessage={usersAdmin.userEditMessage}
        updatingUserId={usersAdmin.updatingUserId}
        deletingUserId={usersAdmin.deletingUserId}
        userPasswordResets={usersAdmin.userPasswordResets}
        resettingPasswordUserId={usersAdmin.resettingPasswordUserId}
        onNewUserEmailChange={usersAdmin.setNewUserEmail}
        onNewUserPasswordChange={usersAdmin.setNewUserPassword}
        onNewUserFullNameChange={usersAdmin.setNewUserFullName}
        onNewUserIsActiveChange={usersAdmin.setNewUserIsActive}
        onNewUserIsSuperuserChange={usersAdmin.setNewUserIsSuperuser}
        onCreateUser={usersAdmin.handleCreateUser}
        onUpdateUserEdit={usersAdmin.updateUserEdit}
        onUpdateUser={usersAdmin.handleUpdateUser}
        onDeleteUser={usersAdmin.handleDeleteUser}
        onUpdateUserPasswordReset={usersAdmin.updateUserPasswordReset}
        onResetUserPassword={usersAdmin.handleResetUserPassword}
      />
    </section>
  );
}
