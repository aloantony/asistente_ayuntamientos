import type { FormEvent } from "react";
import type { User, UserEditState } from "./types";

export type UsersAdminProps = {
  currentUser: User;
  adminUsers: User[];
  isLoadingAdmin: boolean;
  newUserEmail: string;
  newUserPassword: string;
  newUserFullName: string;
  newUserIsActive: boolean;
  newUserIsSuperuser: boolean;
  userFormError: string;
  isCreatingUser: boolean;
  userEdits: Record<number, UserEditState>;
  userEditError: string;
  userEditMessage: string;
  updatingUserId: number | null;
  deletingUserId: number | null;
  onNewUserEmailChange: (email: string) => void;
  onNewUserPasswordChange: (password: string) => void;
  onNewUserFullNameChange: (fullName: string) => void;
  onNewUserIsActiveChange: (isActive: boolean) => void;
  onNewUserIsSuperuserChange: (isSuperuser: boolean) => void;
  onCreateUser: (event: FormEvent<HTMLFormElement>) => void;
  onUpdateUserEdit: (
    userId: number,
    updates: Partial<UserEditState>,
  ) => void;
  onUpdateUser: (userId: number) => void;
  onDeleteUser: (adminUser: User) => void;
};

export function UsersAdmin({
  currentUser,
  adminUsers,
  isLoadingAdmin,
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
  onNewUserEmailChange,
  onNewUserPasswordChange,
  onNewUserFullNameChange,
  onNewUserIsActiveChange,
  onNewUserIsSuperuserChange,
  onCreateUser,
  onUpdateUserEdit,
  onUpdateUser,
  onDeleteUser,
}: UsersAdminProps) {
  return (
    <div className="admin-section">
      <div className="section-header">
        <h3>Usuarios</h3>
        {isLoadingAdmin ? (
          <p className="small-muted">Cargando usuarios.</p>
        ) : null}
      </div>

      <div className="table-wrapper">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Email</th>
              <th>Nombre completo</th>
              <th>Grupos</th>
              <th>Activo</th>
              <th>Superusuario</th>
              <th>Acción</th>
            </tr>
          </thead>
          <tbody>
            {adminUsers.length > 0 ? (
              adminUsers.map((adminUser) => {
                const edit = userEdits[adminUser.id] ?? {
                  full_name: adminUser.full_name,
                  is_active: adminUser.is_active,
                  is_superuser: adminUser.is_superuser,
                };
                const assignedGroups = adminUser.groups ?? [];
                const isCurrentUser = currentUser.id === adminUser.id;

                return (
                  <tr key={adminUser.id}>
                    <td>{adminUser.id}</td>
                    <td>{adminUser.email}</td>
                    <td>
                      <input
                        aria-label={`Nombre completo de ${adminUser.email}`}
                        className="table-input"
                        onChange={(event) =>
                          onUpdateUserEdit(adminUser.id, {
                            full_name: event.target.value,
                          })
                        }
                        type="text"
                        value={edit.full_name}
                      />
                    </td>
                    <td>
                      {assignedGroups.length > 0 ? (
                        <div className="tag-list">
                          {assignedGroups.map((group) => (
                            <span className="tag" key={group.id}>
                              {group.name}
                            </span>
                          ))}
                        </div>
                      ) : (
                        <span className="small-muted">Sin grupos</span>
                      )}
                    </td>
                    <td>
                      <label className="table-checkbox">
                        <input
                          checked={edit.is_active}
                          onChange={(event) =>
                            onUpdateUserEdit(adminUser.id, {
                              is_active: event.target.checked,
                            })
                          }
                          type="checkbox"
                        />
                        Activo
                      </label>
                    </td>
                    <td>
                      <label className="table-checkbox">
                        <input
                          checked={edit.is_superuser}
                          onChange={(event) =>
                            onUpdateUserEdit(adminUser.id, {
                              is_superuser: event.target.checked,
                            })
                          }
                          type="checkbox"
                        />
                        Superusuario
                      </label>
                    </td>
                    <td>
                      <div className="table-actions">
                        <button
                          type="button"
                          onClick={() => onUpdateUser(adminUser.id)}
                          disabled={
                            updatingUserId === adminUser.id ||
                            deletingUserId === adminUser.id ||
                            isLoadingAdmin
                          }
                        >
                          {updatingUserId === adminUser.id
                            ? "Guardando..."
                            : "Guardar"}
                        </button>
                        <button
                          className="danger-button"
                          type="button"
                          onClick={() => onDeleteUser(adminUser)}
                          disabled={
                            isCurrentUser ||
                            deletingUserId === adminUser.id ||
                            updatingUserId === adminUser.id ||
                            isLoadingAdmin
                          }
                          title={
                            isCurrentUser
                              ? "No puedes eliminar la cuenta de la sesión actual"
                              : undefined
                          }
                        >
                          {deletingUserId === adminUser.id
                            ? "Eliminando..."
                            : "Eliminar"}
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })
            ) : (
              <tr>
                <td colSpan={7}>No hay usuarios para mostrar.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {userEditError ? <p className="error-message">{userEditError}</p> : null}
      {userEditMessage ? (
        <p className="success-message">{userEditMessage}</p>
      ) : null}

      <form className="admin-form" onSubmit={onCreateUser}>
        <h4>Crear usuario</h4>
        <div className="form-grid">
          <label>
            Email
            <input
              autoComplete="email"
              name="new-user-email"
              onChange={(event) => onNewUserEmailChange(event.target.value)}
              required
              type="email"
              value={newUserEmail}
            />
          </label>

          <label>
            Contraseña
            <input
              autoComplete="new-password"
              minLength={8}
              name="new-user-password"
              onChange={(event) => onNewUserPasswordChange(event.target.value)}
              required
              type="password"
              value={newUserPassword}
            />
          </label>

          <label>
            Nombre completo
            <input
              name="new-user-full-name"
              onChange={(event) => onNewUserFullNameChange(event.target.value)}
              required
              type="text"
              value={newUserFullName}
            />
          </label>
        </div>

        <div className="checkbox-row">
          <label className="checkbox-label">
            <input
              checked={newUserIsActive}
              onChange={(event) => onNewUserIsActiveChange(event.target.checked)}
              type="checkbox"
            />
            Activo
          </label>
          <label className="checkbox-label">
            <input
              checked={newUserIsSuperuser}
              onChange={(event) =>
                onNewUserIsSuperuserChange(event.target.checked)
              }
              type="checkbox"
            />
            Superusuario
          </label>
        </div>

        {userFormError ? (
          <p className="error-message">{userFormError}</p>
        ) : null}

        <button type="submit" disabled={isCreatingUser}>
          {isCreatingUser ? "Creando..." : "Crear usuario"}
        </button>
      </form>
    </div>
  );
}
