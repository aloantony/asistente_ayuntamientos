import type { FormEvent } from "react";
import type {
  Group,
  MembershipAction,
  Permission,
  Role,
  RoleEditState,
} from "./types";

export type RbacAdminProps = {
  permissions: Permission[];
  roles: Role[];
  groups: Group[];
  isLoadingAdmin: boolean;
  newRoleName: string;
  newRoleDescription: string;
  roleFormError: string;
  isCreatingRole: boolean;
  roleEdits: Record<number, RoleEditState>;
  roleEditError: string;
  roleEditMessage: string;
  updatingRoleId: number | null;
  deletingRoleId: number | null;
  rolePermissionRoleId: string;
  rolePermissionPermissionId: string;
  rolePermissionError: string;
  rolePermissionMessage: string;
  isUpdatingRolePermission: boolean;
  groupRoleGroupId: string;
  groupRoleRoleId: string;
  groupRoleError: string;
  groupRoleMessage: string;
  isUpdatingGroupRole: boolean;
  bootstrapPermissionsError: string;
  bootstrapPermissionsMessage: string;
  isBootstrappingPermissions: boolean;
  onBootstrapPermissions: () => void;
  onNewRoleNameChange: (name: string) => void;
  onNewRoleDescriptionChange: (description: string) => void;
  onCreateRole: (event: FormEvent<HTMLFormElement>) => void;
  onUpdateRoleEdit: (
    roleId: number,
    updates: Partial<RoleEditState>,
  ) => void;
  onUpdateRole: (roleId: number) => void;
  onDeleteRole: (role: Role) => void;
  onRolePermissionRoleIdChange: (roleId: string) => void;
  onRolePermissionPermissionIdChange: (permissionId: string) => void;
  onUpdateRolePermission: (action: MembershipAction) => void;
  onGroupRoleGroupIdChange: (groupId: string) => void;
  onGroupRoleRoleIdChange: (roleId: string) => void;
  onUpdateGroupRole: (action: MembershipAction) => void;
};

export function RbacAdmin({
  permissions,
  roles,
  groups,
  isLoadingAdmin,
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
  onBootstrapPermissions,
  onNewRoleNameChange,
  onNewRoleDescriptionChange,
  onCreateRole,
  onUpdateRoleEdit,
  onUpdateRole,
  onDeleteRole,
  onRolePermissionRoleIdChange,
  onRolePermissionPermissionIdChange,
  onUpdateRolePermission,
  onGroupRoleGroupIdChange,
  onGroupRoleRoleIdChange,
  onUpdateGroupRole,
}: RbacAdminProps) {
  return (
    <div className="admin-section">
      <div className="section-header">
        <h3>Permisos y roles</h3>
        <button
          className="secondary-button"
          type="button"
          onClick={onBootstrapPermissions}
          disabled={isBootstrappingPermissions || isLoadingAdmin}
        >
          {isBootstrappingPermissions
            ? "Inicializando..."
            : "Inicializar permisos base"}
        </button>
      </div>

      {bootstrapPermissionsError ? (
        <p className="error-message">{bootstrapPermissionsError}</p>
      ) : null}
      {bootstrapPermissionsMessage ? (
        <p className="success-message">{bootstrapPermissionsMessage}</p>
      ) : null}

      <div>
        <h4>Permisos</h4>
        {permissions.length > 0 ? (
          <ul className="compact-list permission-list">
            {permissions.map((permission) => (
              <li key={permission.id}>
                <strong>{permission.code}</strong>
                {permission.description ? (
                  <span>{permission.description}</span>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="small-muted">No hay permisos para mostrar.</p>
        )}
      </div>

      <div className="table-wrapper">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Nombre</th>
              <th>Descripción</th>
              <th>Permisos</th>
              <th>Acción</th>
            </tr>
          </thead>
          <tbody>
            {roles.length > 0 ? (
              roles.map((role) => {
                const edit = roleEdits[role.id] ?? {
                  name: role.name,
                  description: role.description ?? "",
                };

                return (
                  <tr key={role.id}>
                    <td>{role.id}</td>
                    <td>
                      <input
                        aria-label={`Nombre del rol ${role.name}`}
                        className="table-input"
                        onChange={(event) =>
                          onUpdateRoleEdit(role.id, {
                            name: event.target.value,
                          })
                        }
                        type="text"
                        value={edit.name}
                      />
                    </td>
                    <td>
                      <textarea
                        aria-label={`Descripción del rol ${role.name}`}
                        className="table-textarea"
                        onChange={(event) =>
                          onUpdateRoleEdit(role.id, {
                            description: event.target.value,
                          })
                        }
                        rows={2}
                        value={edit.description}
                      />
                    </td>
                    <td>
                      {role.permissions.length > 0 ? (
                        <div className="tag-list">
                          {role.permissions.map((permission) => (
                            <span className="tag" key={permission.id}>
                              {permission.code}
                            </span>
                          ))}
                        </div>
                      ) : (
                        <span className="small-muted">Sin permisos</span>
                      )}
                    </td>
                    <td>
                      <div className="table-actions">
                        <button
                          type="button"
                          onClick={() => onUpdateRole(role.id)}
                          disabled={
                            updatingRoleId === role.id ||
                            deletingRoleId === role.id ||
                            isLoadingAdmin
                          }
                        >
                          {updatingRoleId === role.id
                            ? "Guardando..."
                            : "Guardar"}
                        </button>
                        <button
                          className="danger-button"
                          type="button"
                          onClick={() => onDeleteRole(role)}
                          disabled={
                            deletingRoleId === role.id ||
                            updatingRoleId === role.id ||
                            isLoadingAdmin
                          }
                        >
                          {deletingRoleId === role.id
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
                <td colSpan={5}>No hay roles para mostrar.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {roleEditError ? <p className="error-message">{roleEditError}</p> : null}
      {roleEditMessage ? (
        <p className="success-message">{roleEditMessage}</p>
      ) : null}

      <form className="admin-form" onSubmit={onCreateRole}>
        <h4>Crear rol</h4>
        <div className="form-grid">
          <label>
            Nombre
            <input
              name="new-role-name"
              onChange={(event) => onNewRoleNameChange(event.target.value)}
              required
              type="text"
              value={newRoleName}
            />
          </label>

          <label>
            Descripción
            <textarea
              name="new-role-description"
              onChange={(event) =>
                onNewRoleDescriptionChange(event.target.value)
              }
              rows={3}
              value={newRoleDescription}
            />
          </label>
        </div>

        {roleFormError ? (
          <p className="error-message">{roleFormError}</p>
        ) : null}

        <button type="submit" disabled={isCreatingRole}>
          {isCreatingRole ? "Creando..." : "Crear rol"}
        </button>
      </form>

      <div>
        <h4>Permisos de rol</h4>
        <div className="membership-controls">
          <label>
            Rol
            <select
              onChange={(event) =>
                onRolePermissionRoleIdChange(event.target.value)
              }
              value={rolePermissionRoleId}
            >
              <option value="">Selecciona un rol</option>
              {roles.map((role) => (
                <option key={role.id} value={role.id}>
                  {role.name}
                </option>
              ))}
            </select>
          </label>

          <label>
            Permiso
            <select
              onChange={(event) =>
                onRolePermissionPermissionIdChange(event.target.value)
              }
              value={rolePermissionPermissionId}
            >
              <option value="">Selecciona un permiso</option>
              {permissions.map((permission) => (
                <option key={permission.id} value={permission.id}>
                  {permission.code}
                </option>
              ))}
            </select>
          </label>
        </div>

        {rolePermissionError ? (
          <p className="error-message">{rolePermissionError}</p>
        ) : null}
        {rolePermissionMessage ? (
          <p className="success-message">{rolePermissionMessage}</p>
        ) : null}

        <div className="button-row">
          <button
            type="button"
            onClick={() => onUpdateRolePermission("add")}
            disabled={isUpdatingRolePermission}
          >
            Asignar permiso
          </button>
          <button
            className="secondary-button"
            type="button"
            onClick={() => onUpdateRolePermission("remove")}
            disabled={isUpdatingRolePermission}
          >
            Quitar permiso
          </button>
        </div>
      </div>

      <div>
        <h4>Roles de grupo</h4>
        <div className="membership-controls">
          <label>
            Grupo
            <select
              onChange={(event) => onGroupRoleGroupIdChange(event.target.value)}
              value={groupRoleGroupId}
            >
              <option value="">Selecciona un grupo</option>
              {groups.map((group) => (
                <option key={group.id} value={group.id}>
                  {group.name}
                </option>
              ))}
            </select>
          </label>

          <label>
            Rol
            <select
              onChange={(event) => onGroupRoleRoleIdChange(event.target.value)}
              value={groupRoleRoleId}
            >
              <option value="">Selecciona un rol</option>
              {roles.map((role) => (
                <option key={role.id} value={role.id}>
                  {role.name}
                </option>
              ))}
            </select>
          </label>
        </div>

        {groupRoleError ? (
          <p className="error-message">{groupRoleError}</p>
        ) : null}
        {groupRoleMessage ? (
          <p className="success-message">{groupRoleMessage}</p>
        ) : null}

        <div className="button-row">
          <button
            type="button"
            onClick={() => onUpdateGroupRole("add")}
            disabled={isUpdatingGroupRole}
          >
            Asignar rol
          </button>
          <button
            className="secondary-button"
            type="button"
            onClick={() => onUpdateGroupRole("remove")}
            disabled={isUpdatingGroupRole}
          >
            Quitar rol
          </button>
        </div>
      </div>
    </div>
  );
}
