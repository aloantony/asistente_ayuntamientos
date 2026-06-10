import type { FormEvent } from "react";
import {
  formatDocumentStatus,
  formatFileSize,
  formatProjectStatus,
  formatUserOption,
  formatOrganizationOption,
  PROJECT_STATUSES,
  userHasPermission,
  type Document,
  type Group,
  type MembershipAction,
  type OrganizationSummary,
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
  organizations: OrganizationSummary[];
  isLoadingProjects: boolean;
  projectError: string;
  newProjectName: string;
  newProjectDescription: string;
  newProjectStatus: ProjectStatus;
  newProjectOrganizationId: string;
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
  projectDocuments: Record<number, Document[]>;
  projectDocumentErrors: Record<number, string>;
  isLoadingDocuments: boolean;
  includeArchivedDocuments: boolean;
  uploadingDocumentProjectId: number | null;
  archivingDocumentId: number | null;
  documentError: string;
  documentMessage: string;
  onRefresh: () => void;
  onNewProjectNameChange: (name: string) => void;
  onNewProjectDescriptionChange: (description: string) => void;
  onNewProjectStatusChange: (status: ProjectStatus) => void;
  onNewProjectOrganizationIdChange: (organizationId: string) => void;
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
  onUploadDocument: (
    projectId: number,
    event: FormEvent<HTMLFormElement>,
  ) => void;
  onDownloadDocument: (document: Document) => void;
  onArchiveDocument: (document: Document) => void;
  onIncludeArchivedDocumentsChange: (includeArchived: boolean) => void;
};

export function ProjectsPanel({
  user,
  projects,
  adminUsers,
  groups,
  organizations,
  isLoadingProjects,
  projectError,
  newProjectName,
  newProjectDescription,
  newProjectStatus,
  newProjectOrganizationId,
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
  projectDocuments,
  projectDocumentErrors,
  isLoadingDocuments,
  includeArchivedDocuments,
  uploadingDocumentProjectId,
  archivingDocumentId,
  documentError,
  documentMessage,
  onRefresh,
  onNewProjectNameChange,
  onNewProjectDescriptionChange,
  onNewProjectStatusChange,
  onNewProjectOrganizationIdChange,
  onCreateProject,
  onUpdateProjectEdit,
  onUpdateProject,
  onProjectMembershipProjectIdChange,
  onProjectMembershipUserIdChange,
  onProjectMembershipGroupIdChange,
  onUpdateProjectUserMembership,
  onUpdateProjectGroupMembership,
  onUploadDocument,
  onDownloadDocument,
  onArchiveDocument,
  onIncludeArchivedDocumentsChange,
}: ProjectsPanelProps) {
  const canCreateProjects = userHasPermission(user, "projects.create");
  const canEditProjects = userHasPermission(user, "projects.edit");
  const canArchiveProjects = userHasPermission(user, "projects.archive");
  const canViewDocuments =
    userHasPermission(user, "documents.view") ||
    userHasPermission(user, "documents.manage");
  const canUploadDocuments =
    userHasPermission(user, "documents.upload") ||
    userHasPermission(user, "documents.manage");
  const canArchiveDocuments =
    userHasPermission(user, "documents.archive") ||
    userHasPermission(user, "documents.manage");
  const showProjectMembershipControls = userHasPermission(
    user,
    "projects.manage_members",
  );
  const selectedMembershipProject = projects.find(
    (project) => String(project.id) === projectMembershipProjectId,
  );
  const projectMembershipUsers = selectedMembershipProject
    ? adminUsers.filter((adminUser) =>
        (adminUser.organizations ?? []).some(
          (organization) =>
            organization.id === selectedMembershipProject.organization_id,
        ),
      )
    : adminUsers;
  const projectMembershipGroups = selectedMembershipProject
    ? groups.filter(
        (group) =>
          group.organization_id === selectedMembershipProject.organization_id,
      )
    : groups;

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
                <th>Organización</th>
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
                        <span className="tag">{project.organization.name}</span>
                      </td>
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
                  <td colSpan={canEditProjects ? 8 : 7}>
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

      {canViewDocuments || canUploadDocuments ? (
        <div className="admin-section documents-section">
          <div className="section-header">
            <h3>Documentos</h3>
            <div className="documents-toolbar">
              {isLoadingDocuments ? (
                <p className="small-muted">Cargando documentos.</p>
              ) : null}
              {canViewDocuments ? (
                <label className="checkbox-label">
                  <input
                    checked={includeArchivedDocuments}
                    onChange={(event) =>
                      onIncludeArchivedDocumentsChange(event.target.checked)
                    }
                    type="checkbox"
                  />
                  Mostrar archivados
                </label>
              ) : null}
            </div>
          </div>

          {documentError ? (
            <p className="error-message">{documentError}</p>
          ) : null}
          {documentMessage ? (
            <p className="success-message">{documentMessage}</p>
          ) : null}

          {projects.length > 0 ? (
            <div className="project-documents-list">
              {projects.map((project) => {
                const documents = projectDocuments[project.id] ?? [];
                const projectDocumentError =
                  projectDocumentErrors[project.id];

                return (
                  <div className="project-documents-row" key={project.id}>
                    <div className="project-documents-heading">
                      <div>
                        <h4>{project.name}</h4>
                        <p className="small-muted">
                          {project.organization.name}
                        </p>
                      </div>

                      {canUploadDocuments ? (
                        <form
                          className="document-upload-form"
                          onSubmit={(event) =>
                            onUploadDocument(project.id, event)
                          }
                        >
                          <input
                            aria-label={`Subir documento a ${project.name}`}
                            name="file"
                            type="file"
                            accept=".pdf,.png,.jpg,.jpeg,.txt,.doc,.docx,.xls,.xlsx,application/pdf,image/png,image/jpeg,text/plain,application/msword,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                          />
                          <button
                            type="submit"
                            disabled={
                              uploadingDocumentProjectId === project.id
                            }
                          >
                            {uploadingDocumentProjectId === project.id
                              ? "Subiendo..."
                              : "Subir"}
                          </button>
                        </form>
                      ) : null}
                    </div>

                    {canViewDocuments ? (
                      <>
                        {projectDocumentError ? (
                          <p className="error-message">
                            {projectDocumentError}
                          </p>
                        ) : null}

                        {documents.length > 0 ? (
                          <ul className="document-list">
                            {documents.map((document) => (
                              <li key={document.id}>
                                <div>
                                  <strong>{document.original_filename}</strong>
                                  <span>
                                    {formatFileSize(document.size_bytes)} -{" "}
                                    {document.content_type} -{" "}
                                    {formatDocumentStatus(document.status)}
                                  </span>
                                </div>
                                <div className="table-actions">
                                  <button
                                    className="secondary-button"
                                    type="button"
                                    onClick={() =>
                                      onDownloadDocument(document)
                                    }
                                  >
                                    Descargar
                                  </button>
                                  {canArchiveDocuments &&
                                  document.status === "active" ? (
                                    <button
                                      className="danger-button"
                                      type="button"
                                      onClick={() =>
                                        onArchiveDocument(document)
                                      }
                                      disabled={
                                        archivingDocumentId === document.id
                                      }
                                    >
                                      {archivingDocumentId === document.id
                                        ? "Archivando..."
                                        : "Archivar"}
                                    </button>
                                  ) : null}
                                </div>
                              </li>
                            ))}
                          </ul>
                        ) : (
                          <p className="small-muted">
                            No hay documentos para mostrar.
                          </p>
                        )}
                      </>
                    ) : null}
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="small-muted">
              No hay proyectos disponibles para documentos.
            </p>
          )}
        </div>
      ) : null}

      {canCreateProjects || showProjectMembershipControls ? (
        <>
          {canCreateProjects ? (
            <div className="admin-section">
              <form className="admin-form" onSubmit={onCreateProject}>
                <h3>Crear proyecto</h3>
                <div className="form-grid">
                  <label>
                    Organización
                    <select
                      name="new-project-organization"
                      onChange={(event) =>
                        onNewProjectOrganizationIdChange(event.target.value)
                      }
                      required
                      value={newProjectOrganizationId}
                    >
                      <option value="">Selecciona una organización</option>
                      {organizations.map((organization) => (
                        <option key={organization.id} value={organization.id}>
                          {formatOrganizationOption(organization)}
                        </option>
                      ))}
                    </select>
                  </label>

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
                    {projectMembershipUsers.map((adminUser) => (
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
                    {projectMembershipGroups.map((group) => (
                      <option key={group.id} value={group.id}>
                        {group.name} - {group.organization.name}
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
