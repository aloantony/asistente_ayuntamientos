import type { FormEvent } from "react";
import {
  formatProjectStatus,
  formatUserOption,
  PROJECT_STATUSES,
  userHasPermission,
  type Group,
  type MembershipAction,
  type Project,
  type ProjectEditState,
  type ProjectStatus,
  type User,
} from "./types";

type ProjectsPanelProps = {
  user: User;
  projects: Project[];
  adminUsers: User[];
  groups: Group[];
  isLoadingProjects: boolean;
  projectError: string;
  newProjectName: string;
  newProjectDescription: string;
  newProjectStatus: ProjectStatus;
  projectFormError: string;
  isCreatingProject: boolean;
  projectEdits: Record<number, ProjectEditState>;
  projectEditError: string;
  projectEditMessage: string;
  updatingProjectId: number | null;
  projectMembershipProjectId: string;
  projectMembershipUserId: string;
  projectMembershipGroupId: string;
  projectMembershipError: string;
  projectMembershipMessage: string;
  isUpdatingProjectMembership: boolean;
  onRefresh: () => void;
  onNewProjectNameChange: (name: string) => void;
  onNewProjectDescriptionChange: (description: string) => void;
  onNewProjectStatusChange: (status: ProjectStatus) => void;
  onCreateProject: (event: FormEvent<HTMLFormElement>) => void;
  onUpdateProjectEdit: (
    projectId: number,
    updates: Partial<ProjectEditState>,
  ) => void;
  onUpdateProject: (projectId: number) => void;
  onProjectMembershipProjectIdChange: (projectId: string) => void;
  onProjectMembershipUserIdChange: (userId: string) => void;
  onProjectMembershipGroupIdChange: (groupId: string) => void;
  onUpdateProjectUserMembership: (action: MembershipAction) => void;
  onUpdateProjectGroupMembership: (action: MembershipAction) => void;
};

export function ProjectsPanel({
  user,
  projects,
  adminUsers,
  groups,
  isLoadingProjects,
  projectError,
  newProjectName,
  newProjectDescription,
  newProjectStatus,
  projectFormError,
  isCreatingProject,
  projectEdits,
  projectEditError,
  projectEditMessage,
  updatingProjectId,
  projectMembershipProjectId,
  projectMembershipUserId,
  projectMembershipGroupId,
  projectMembershipError,
  projectMembershipMessage,
  isUpdatingProjectMembership,
  onRefresh,
  onNewProjectNameChange,
  onNewProjectDescriptionChange,
  onNewProjectStatusChange,
  onCreateProject,
  onUpdateProjectEdit,
  onUpdateProject,
  onProjectMembershipProjectIdChange,
  onProjectMembershipUserIdChange,
  onProjectMembershipGroupIdChange,
  onUpdateProjectUserMembership,
  onUpdateProjectGroupMembership,
}: ProjectsPanelProps) {
  const canCreateProjects = userHasPermission(user, "projects.create");
  const canEditProjects = userHasPermission(user, "projects.edit");
  const canArchiveProjects = userHasPermission(user, "projects.archive");
  const showProjectMembershipControls =
    user.is_superuser && userHasPermission(user, "projects.manage_members");

  return (
    <section className="panel admin-panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">Trabajo</p>
          <h2>Proyectos</h2>
        </div>
        <button
          className="secondary-button"
          type="button"
          onClick={onRefresh}
          disabled={isLoadingProjects}
        >
          {isLoadingProjects ? "Cargando..." : "Actualizar"}
        </button>
      </div>

      {projectError ? <p className="error-message">{projectError}</p> : null}

      {!user.is_superuser &&
      !canCreateProjects &&
      !isLoadingProjects &&
      projects.length === 0 ? (
        <p className="small-muted">
          No tienes proyectos accesibles. Un administrador puede asignarte
          directamente o mediante un grupo.
        </p>
      ) : null}

      <div className="admin-section">
        <div className="section-header">
          <h3>Listado</h3>
          {isLoadingProjects ? (
            <p className="small-muted">Cargando proyectos.</p>
          ) : null}
        </div>

        <div className="table-wrapper">
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Nombre</th>
                <th>Descripción</th>
                <th>Estado</th>
                <th>Usuarios</th>
                <th>Grupos</th>
                {canEditProjects ? <th>Acción</th> : null}
              </tr>
            </thead>
            <tbody>
              {projects.length > 0 ? (
                projects.map((project) => {
                  const edit = projectEdits[project.id] ?? {
                    name: project.name,
                    description: project.description ?? "",
                    status: project.status,
                  };
                  const assignedUsers = project.users ?? [];
                  const assignedGroups = project.groups ?? [];

                  return (
                    <tr key={project.id}>
                      <td>{project.id}</td>
                      <td>
                        {canEditProjects ? (
                          <input
                            aria-label={`Nombre del proyecto ${project.name}`}
                            className="table-input"
                            onChange={(event) =>
                              onUpdateProjectEdit(project.id, {
                                name: event.target.value,
                              })
                            }
                            type="text"
                            value={edit.name}
                          />
                        ) : (
                          <strong>{project.name}</strong>
                        )}
                      </td>
                      <td>
                        {canEditProjects ? (
                          <textarea
                            aria-label={`Descripción del proyecto ${project.name}`}
                            className="table-textarea"
                            onChange={(event) =>
                              onUpdateProjectEdit(project.id, {
                                description: event.target.value,
                              })
                            }
                            rows={2}
                            value={edit.description}
                          />
                        ) : project.description ? (
                          project.description
                        ) : (
                          <span className="small-muted">Sin descripción</span>
                        )}
                      </td>
                      <td>
                        {canEditProjects ? (
                          <select
                            aria-label={`Estado del proyecto ${project.name}`}
                            className="table-input"
                            onChange={(event) =>
                              onUpdateProjectEdit(project.id, {
                                status: event.target.value as ProjectStatus,
                              })
                            }
                            value={edit.status}
                          >
                            {PROJECT_STATUSES.map((status) => (
                              <option
                                disabled={
                                  status === "archived" &&
                                  !canArchiveProjects &&
                                  edit.status !== "archived"
                                }
                                key={status}
                                value={status}
                              >
                                {formatProjectStatus(status)}
                              </option>
                            ))}
                          </select>
                        ) : (
                          <span className="tag">
                            {formatProjectStatus(project.status)}
                          </span>
                        )}
                      </td>
                      <td>
                        {assignedUsers.length > 0 ? (
                          <ul className="compact-list">
                            {assignedUsers.map((projectUser) => (
                              <li key={projectUser.id}>
                                <strong>{projectUser.full_name}</strong>
                                <span>{projectUser.email}</span>
                              </li>
                            ))}
                          </ul>
                        ) : (
                          <span className="small-muted">Sin usuarios</span>
                        )}
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
                      {canEditProjects ? (
                        <td>
                          <button
                            type="button"
                            onClick={() => onUpdateProject(project.id)}
                            disabled={
                              updatingProjectId === project.id ||
                              isLoadingProjects
                            }
                          >
                            {updatingProjectId === project.id
                              ? "Guardando..."
                              : "Guardar"}
                          </button>
                        </td>
                      ) : null}
                    </tr>
                  );
                })
              ) : (
                <tr>
                  <td colSpan={canEditProjects ? 7 : 6}>
                    {canCreateProjects || canEditProjects
                      ? "No hay proyectos para mostrar."
                      : "No tienes proyectos accesibles."}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {projectEditError ? (
          <p className="error-message">{projectEditError}</p>
        ) : null}
        {projectEditMessage ? (
          <p className="success-message">{projectEditMessage}</p>
        ) : null}
      </div>

      {canCreateProjects || showProjectMembershipControls ? (
        <>
          {canCreateProjects ? (
            <div className="admin-section">
              <form className="admin-form" onSubmit={onCreateProject}>
                <h3>Crear proyecto</h3>
                <div className="form-grid">
                  <label>
                    Nombre
                    <input
                      name="new-project-name"
                      onChange={(event) =>
                        onNewProjectNameChange(event.target.value)
                      }
                      required
                      type="text"
                      value={newProjectName}
                    />
                  </label>

                  <label>
                    Estado
                    <select
                      name="new-project-status"
                      onChange={(event) =>
                        onNewProjectStatusChange(
                          event.target.value as ProjectStatus,
                        )
                      }
                      value={newProjectStatus}
                    >
                      {PROJECT_STATUSES.map((status) => (
                        <option key={status} value={status}>
                          {formatProjectStatus(status)}
                        </option>
                      ))}
                    </select>
                  </label>

                  <label>
                    Descripción
                    <textarea
                      name="new-project-description"
                      onChange={(event) =>
                        onNewProjectDescriptionChange(event.target.value)
                      }
                      rows={3}
                      value={newProjectDescription}
                    />
                  </label>
                </div>

                {projectFormError ? (
                  <p className="error-message">{projectFormError}</p>
                ) : null}

                <button type="submit" disabled={isCreatingProject}>
                  {isCreatingProject ? "Creando..." : "Crear proyecto"}
                </button>
              </form>
            </div>
          ) : null}

          {showProjectMembershipControls ? (
            <div className="admin-section">
              <h3>Pertenencia a proyectos</h3>

              <div className="membership-controls">
                <label>
                  Proyecto
                  <select
                    onChange={(event) =>
                      onProjectMembershipProjectIdChange(event.target.value)
                    }
                    value={projectMembershipProjectId}
                  >
                    <option value="">Selecciona un proyecto</option>
                    {projects.map((project) => (
                      <option key={project.id} value={project.id}>
                        {project.name}
                      </option>
                    ))}
                  </select>
                </label>

                <label>
                  Usuario
                  <select
                    onChange={(event) =>
                      onProjectMembershipUserIdChange(event.target.value)
                    }
                    value={projectMembershipUserId}
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
                      onProjectMembershipGroupIdChange(event.target.value)
                    }
                    value={projectMembershipGroupId}
                  >
                    <option value="">Selecciona un grupo</option>
                    {groups.map((group) => (
                      <option key={group.id} value={group.id}>
                        {group.name}
                      </option>
                    ))}
                  </select>
                </label>
              </div>

              {projectMembershipError ? (
                <p className="error-message">{projectMembershipError}</p>
              ) : null}
              {projectMembershipMessage ? (
                <p className="success-message">{projectMembershipMessage}</p>
              ) : null}

              <div className="button-row">
                <button
                  type="button"
                  onClick={() => onUpdateProjectUserMembership("add")}
                  disabled={isUpdatingProjectMembership}
                >
                  Añadir usuario
                </button>
                <button
                  className="secondary-button"
                  type="button"
                  onClick={() => onUpdateProjectUserMembership("remove")}
                  disabled={isUpdatingProjectMembership}
                >
                  Quitar usuario
                </button>
                <button
                  type="button"
                  onClick={() => onUpdateProjectGroupMembership("add")}
                  disabled={isUpdatingProjectMembership}
                >
                  Añadir grupo
                </button>
                <button
                  className="secondary-button"
                  type="button"
                  onClick={() => onUpdateProjectGroupMembership("remove")}
                  disabled={isUpdatingProjectMembership}
                >
                  Quitar grupo
                </button>
              </div>
            </div>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
