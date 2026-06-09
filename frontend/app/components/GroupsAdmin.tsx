import type { FormEvent } from "react";
import type { Group, GroupEditState } from "./types";

export type GroupsAdminProps = {
  groups: Group[];
  isLoadingAdmin: boolean;
  newGroupName: string;
  newGroupDescription: string;
  groupFormError: string;
  isCreatingGroup: boolean;
  groupEdits: Record<number, GroupEditState>;
  groupEditError: string;
  groupEditMessage: string;
  updatingGroupId: number | null;
  deletingGroupId: number | null;
  onNewGroupNameChange: (name: string) => void;
  onNewGroupDescriptionChange: (description: string) => void;
  onCreateGroup: (event: FormEvent<HTMLFormElement>) => void;
  onUpdateGroupEdit: (
    groupId: number,
    updates: Partial<GroupEditState>,
  ) => void;
  onUpdateGroup: (groupId: number) => void;
  onDeleteGroup: (group: Group) => void;
};

export function GroupsAdmin({
  groups,
  isLoadingAdmin,
  newGroupName,
  newGroupDescription,
  groupFormError,
  isCreatingGroup,
  groupEdits,
  groupEditError,
  groupEditMessage,
  updatingGroupId,
  deletingGroupId,
  onNewGroupNameChange,
  onNewGroupDescriptionChange,
  onCreateGroup,
  onUpdateGroupEdit,
  onUpdateGroup,
  onDeleteGroup,
}: GroupsAdminProps) {
  return (
    <div className="admin-section">
      <div className="section-header">
        <h3>Grupos</h3>
        {isLoadingAdmin ? (
          <p className="small-muted">Cargando grupos.</p>
        ) : null}
      </div>

      <div className="table-wrapper">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Nombre</th>
              <th>Descripción</th>
              <th>Usuarios</th>
              <th>Acción</th>
            </tr>
          </thead>
          <tbody>
            {groups.length > 0 ? (
              groups.map((group) => {
                const edit = groupEdits[group.id] ?? {
                  name: group.name,
                  description: group.description ?? "",
                };
                const assignedUsers = group.users ?? [];

                return (
                  <tr key={group.id}>
                    <td>{group.id}</td>
                    <td>
                      <input
                        aria-label={`Nombre del grupo ${group.name}`}
                        className="table-input"
                        onChange={(event) =>
                          onUpdateGroupEdit(group.id, {
                            name: event.target.value,
                          })
                        }
                        type="text"
                        value={edit.name}
                      />
                    </td>
                    <td>
                      <textarea
                        aria-label={`Descripción del grupo ${group.name}`}
                        className="table-textarea"
                        onChange={(event) =>
                          onUpdateGroupEdit(group.id, {
                            description: event.target.value,
                          })
                        }
                        rows={2}
                        value={edit.description}
                      />
                    </td>
                    <td>
                      {assignedUsers.length > 0 ? (
                        <ul className="compact-list">
                          {assignedUsers.map((groupUser) => (
                            <li key={groupUser.id}>
                              <strong>{groupUser.full_name}</strong>
                              <span>{groupUser.email}</span>
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <span className="small-muted">Sin usuarios</span>
                      )}
                    </td>
                    <td>
                      <div className="table-actions">
                        <button
                          type="button"
                          onClick={() => onUpdateGroup(group.id)}
                          disabled={
                            updatingGroupId === group.id ||
                            deletingGroupId === group.id ||
                            isLoadingAdmin
                          }
                        >
                          {updatingGroupId === group.id
                            ? "Guardando..."
                            : "Guardar"}
                        </button>
                        <button
                          className="danger-button"
                          type="button"
                          onClick={() => onDeleteGroup(group)}
                          disabled={
                            deletingGroupId === group.id ||
                            updatingGroupId === group.id ||
                            isLoadingAdmin
                          }
                        >
                          {deletingGroupId === group.id
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
                <td colSpan={5}>No hay grupos para mostrar.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {groupEditError ? (
        <p className="error-message">{groupEditError}</p>
      ) : null}
      {groupEditMessage ? (
        <p className="success-message">{groupEditMessage}</p>
      ) : null}

      <form className="admin-form" onSubmit={onCreateGroup}>
        <h4>Crear grupo</h4>
        <div className="form-grid">
          <label>
            Nombre
            <input
              name="new-group-name"
              onChange={(event) => onNewGroupNameChange(event.target.value)}
              required
              type="text"
              value={newGroupName}
            />
          </label>

          <label>
            Descripción
            <textarea
              name="new-group-description"
              onChange={(event) =>
                onNewGroupDescriptionChange(event.target.value)
              }
              rows={3}
              value={newGroupDescription}
            />
          </label>
        </div>

        {groupFormError ? (
          <p className="error-message">{groupFormError}</p>
        ) : null}

        <button type="submit" disabled={isCreatingGroup}>
          {isCreatingGroup ? "Creando..." : "Crear grupo"}
        </button>
      </form>
    </div>
  );
}
