import type { FormEvent } from "react";
import {
  formatOrganizationOption,
  formatRequirementMessageType,
  formatRequirementPriority,
  formatRequirementSourceType,
  formatRequirementStatus,
  REQUIREMENT_MESSAGE_TYPES,
  REQUIREMENT_PRIORITIES,
  REQUIREMENT_SOURCE_TYPES,
  REQUIREMENT_STATUSES,
  userHasPermission,
  type OrganizationSummary,
  type Project,
  type Requirement,
  type RequirementEditState,
  type RequirementFormState,
  type RequirementMessage,
  type RequirementMessageType,
  type RequirementStatus,
  type User,
} from "./types";

type RequirementTextField = {
  key: Exclude<
    keyof RequirementFormState,
    | "organization_id"
    | "project_id"
    | "title"
    | "priority"
    | "source_type"
  >;
  label: string;
  rows?: number;
  helper?: string;
};

type RequirementsPanelProps = {
  user: User;
  organizations: OrganizationSummary[];
  projects: Project[];
  requirements: Requirement[];
  selectedRequirement: Requirement | null;
  requirementMessages: RequirementMessage[];
  isLoadingRequirements: boolean;
  /** Página 0-indexada de la lista (la URL usa 1-indexada). */
  requirementPage: number;
  requirementTotal: number;
  requirementPageSize: number;
  requirementError: string;
  requirementMessage: string;
  filterOrganizationId: string;
  filterProjectId: string;
  filterStatus: string;
  includeArchivedRequirements: boolean;
  newRequirement: RequirementFormState;
  requirementFormError: string;
  isCreatingRequirement: boolean;
  requirementEdit: RequirementEditState | null;
  requirementEditError: string;
  isUpdatingRequirement: boolean;
  newRequirementMessageBody: string;
  newRequirementMessageType: RequirementMessageType;
  requirementMessagesError: string;
  isCreatingRequirementMessage: boolean;
  onRefresh: () => void;
  onSelectRequirement: (requirementId: number) => void;
  onRequirementPrevPage: () => void;
  onRequirementNextPage: () => void;
  onUpdateNewRequirement: (updates: Partial<RequirementFormState>) => void;
  onUpdateRequirementEdit: (updates: Partial<RequirementEditState>) => void;
  onFilterOrganizationIdChange: (organizationId: string) => void;
  onFilterProjectIdChange: (projectId: string) => void;
  onFilterStatusChange: (status: string) => void;
  onIncludeArchivedRequirementsChange: (includeArchived: boolean) => void;
  onNewRequirementMessageBodyChange: (body: string) => void;
  onNewRequirementMessageTypeChange: (messageType: RequirementMessageType) => void;
  onCreateRequirement: (event: FormEvent<HTMLFormElement>) => void;
  onUpdateRequirement: () => void;
  onChangeRequirementStatus: (status: RequirementStatus) => void;
  onCreateRequirementMessage: (event: FormEvent<HTMLFormElement>) => void;
};

const dictationHelper =
  "Puedes dictar aquí usando el micrófono del teclado del móvil o del navegador si está disponible.";

const requirementTextFields: RequirementTextField[] = [
  { key: "summary", label: "Resumen", rows: 3, helper: dictationHelper },
  { key: "problem", label: "Problema", rows: 3, helper: dictationHelper },
  { key: "current_process", label: "Proceso actual", rows: 3 },
  { key: "desired_process", label: "Proceso deseado", rows: 3 },
  { key: "affected_users", label: "Usuarios afectados", rows: 2 },
  { key: "involved_documents", label: "Documentos implicados", rows: 2 },
  {
    key: "data_sensitivity_notes",
    label: "Notas de sensibilidad de datos",
    rows: 2,
  },
  { key: "legal_notes", label: "Notas legales", rows: 2 },
  { key: "acceptance_criteria", label: "Criterios de aceptación", rows: 3 },
  { key: "open_questions", label: "Preguntas abiertas", rows: 3 },
];

const reviewStatuses: RequirementStatus[] = [
  "in_review",
  "needs_clarification",
  "accepted",
  "rejected",
  "converted",
];

function formatDateTime(value: string) {
  return new Date(value).toLocaleString("es-ES");
}

function getProjectsForOrganization(projects: Project[], organizationId: string) {
  const parsedOrganizationId = Number.parseInt(organizationId, 10);
  if (!Number.isInteger(parsedOrganizationId)) {
    return projects;
  }

  return projects.filter(
    (project) => project.organization_id === parsedOrganizationId,
  );
}

function renderRequirementValue(value: string | null) {
  return value ? value : <span className="small-muted">Sin indicar</span>;
}

export function RequirementsPanel({
  user,
  organizations,
  projects,
  requirements,
  selectedRequirement,
  requirementMessages,
  isLoadingRequirements,
  requirementPage,
  requirementTotal,
  requirementPageSize,
  requirementError,
  requirementMessage,
  filterOrganizationId,
  filterProjectId,
  filterStatus,
  includeArchivedRequirements,
  newRequirement,
  requirementFormError,
  isCreatingRequirement,
  requirementEdit,
  requirementEditError,
  isUpdatingRequirement,
  newRequirementMessageBody,
  newRequirementMessageType,
  requirementMessagesError,
  isCreatingRequirementMessage,
  onRefresh,
  onSelectRequirement,
  onRequirementPrevPage,
  onRequirementNextPage,
  onUpdateNewRequirement,
  onUpdateRequirementEdit,
  onFilterOrganizationIdChange,
  onFilterProjectIdChange,
  onFilterStatusChange,
  onIncludeArchivedRequirementsChange,
  onNewRequirementMessageBodyChange,
  onNewRequirementMessageTypeChange,
  onCreateRequirement,
  onUpdateRequirement,
  onChangeRequirementStatus,
  onCreateRequirementMessage,
}: RequirementsPanelProps) {
  const canCreateRequirements =
    userHasPermission(user, "requirements.create") ||
    userHasPermission(user, "requirements.manage");
  const canEditRequirements =
    userHasPermission(user, "requirements.edit") ||
    userHasPermission(user, "requirements.manage");
  const canReviewRequirements =
    userHasPermission(user, "requirements.review") ||
    userHasPermission(user, "requirements.manage");
  const canArchiveRequirements =
    userHasPermission(user, "requirements.archive") ||
    userHasPermission(user, "requirements.manage");
  const filterProjects = getProjectsForOrganization(projects, filterOrganizationId);
  const newRequirementProjects = getProjectsForOrganization(
    projects,
    newRequirement.organization_id,
  );
  const editRequirementProjects = selectedRequirement
    ? getProjectsForOrganization(projects, String(selectedRequirement.organization_id))
    : projects;
  const requirementPageCount = Math.max(
    1,
    Math.ceil(requirementTotal / requirementPageSize),
  );

  return (
    <section className="panel admin-panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">Producto</p>
          <h2>Necesidades</h2>
        </div>
        <button
          className="secondary-button"
          type="button"
          onClick={onRefresh}
          disabled={isLoadingRequirements}
        >
          {isLoadingRequirements ? "Cargando..." : "Actualizar"}
        </button>
      </div>

      {requirementError ? (
        <p className="error-message">{requirementError}</p>
      ) : null}
      {requirementMessage ? (
        <p className="success-message">{requirementMessage}</p>
      ) : null}

      <div className="admin-section">
        <div className="section-header">
          <h3>Necesidades pendientes</h3>
          {isLoadingRequirements ? (
            <p className="small-muted">Cargando necesidades.</p>
          ) : null}
        </div>

        <div className="requirement-filters">
          <label>
            Organización
            <select
              value={filterOrganizationId}
              onChange={(event) => {
                onFilterOrganizationIdChange(event.target.value);
                onFilterProjectIdChange("");
              }}
            >
              <option value="">Todas</option>
              {organizations.map((organization) => (
                <option key={organization.id} value={organization.id}>
                  {formatOrganizationOption(organization)}
                </option>
              ))}
            </select>
          </label>

          <label>
            Proyecto
            <select
              value={filterProjectId}
              onChange={(event) => onFilterProjectIdChange(event.target.value)}
            >
              <option value="">Todos</option>
              {filterProjects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          </label>

          <label>
            Estado
            <select
              value={filterStatus}
              onChange={(event) => onFilterStatusChange(event.target.value)}
            >
              <option value="">Todos excepto archivados</option>
              {REQUIREMENT_STATUSES.map((status) => (
                <option key={status} value={status}>
                  {formatRequirementStatus(status)}
                </option>
              ))}
            </select>
          </label>

          <label className="checkbox-label requirements-archive-toggle">
            <input
              checked={includeArchivedRequirements}
              onChange={(event) =>
                onIncludeArchivedRequirementsChange(event.target.checked)
              }
              type="checkbox"
            />
            Incluir archivados
          </label>
        </div>

        <div className="button-row">
          <button
            className="secondary-button"
            type="button"
            onClick={onRefresh}
            disabled={isLoadingRequirements}
          >
            Aplicar filtros
          </button>
        </div>

        <div className="requirements-layout">
          <div className="requirement-list">
            {requirements.length > 0 ? (
              requirements.map((requirement) => (
                <button
                  className={
                    selectedRequirement?.id === requirement.id
                      ? "requirement-list-item selected"
                      : "requirement-list-item"
                  }
                  key={requirement.id}
                  type="button"
                  onClick={() => onSelectRequirement(requirement.id)}
                >
                  <strong>{requirement.title}</strong>
                  <span>
                    {requirement.organization.name}
                    {requirement.project ? ` - ${requirement.project.name}` : ""}
                  </span>
                  <span>
                    {formatRequirementStatus(requirement.status)} -{" "}
                    {formatRequirementPriority(requirement.priority)}
                  </span>
                </button>
              ))
            ) : (
              <p className="small-muted">No hay necesidades para mostrar.</p>
            )}

            <div className="pager-row">
              <button
                className="secondary-button"
                type="button"
                onClick={onRequirementPrevPage}
                disabled={requirementPage === 0 || isLoadingRequirements}
              >
                Anterior
              </button>
              <span className="pager-status">
                Página {requirementPage + 1} de {requirementPageCount} (
                {requirementTotal} en total)
              </span>
              <button
                className="secondary-button"
                type="button"
                onClick={onRequirementNextPage}
                disabled={
                  requirementPage + 1 >= requirementPageCount ||
                  isLoadingRequirements
                }
              >
                Siguiente
              </button>
            </div>
          </div>

          <div className="requirement-detail">
            {selectedRequirement ? (
              <>
                <div className="section-header">
                  <div>
                    <h3>{selectedRequirement.title}</h3>
                    <p className="small-muted">
                      {selectedRequirement.organization.name}
                      {selectedRequirement.project
                        ? ` - ${selectedRequirement.project.name}`
                        : ""}
                    </p>
                  </div>
                  <span className="tag">
                    {formatRequirementStatus(selectedRequirement.status)}
                  </span>
                </div>

                <dl className="requirement-fields">
                  <div>
                    <dt>Prioridad</dt>
                    <dd>
                      {formatRequirementPriority(selectedRequirement.priority)}
                    </dd>
                  </div>
                  <div>
                    <dt>Origen</dt>
                    <dd>
                      {formatRequirementSourceType(
                        selectedRequirement.source_type,
                      )}
                    </dd>
                  </div>
                  <div>
                    <dt>Creado por</dt>
                    <dd>
                      {selectedRequirement.created_by
                        ? selectedRequirement.created_by.full_name
                        : "Sin usuario"}
                    </dd>
                  </div>
                  <div>
                    <dt>Revisado por</dt>
                    <dd>
                      {selectedRequirement.reviewed_by
                        ? selectedRequirement.reviewed_by.full_name
                        : "Sin revisar"}
                    </dd>
                  </div>
                  {requirementTextFields.map((field) => (
                    <div key={field.key}>
                      <dt>{field.label}</dt>
                      <dd>{renderRequirementValue(selectedRequirement[field.key])}</dd>
                    </div>
                  ))}
                </dl>

                {canReviewRequirements || canArchiveRequirements ? (
                  <div className="requirement-status-actions">
                    {canReviewRequirements
                      ? reviewStatuses.map((status) => (
                          <button
                            className="secondary-button"
                            disabled={
                              isUpdatingRequirement ||
                              selectedRequirement.status === status
                            }
                            key={status}
                            type="button"
                            onClick={() => onChangeRequirementStatus(status)}
                          >
                            {formatRequirementStatus(status)}
                          </button>
                        ))
                      : null}
                    {canArchiveRequirements &&
                    selectedRequirement.status !== "archived" ? (
                      <button
                        className="danger-button"
                        disabled={isUpdatingRequirement}
                        type="button"
                        onClick={() => onChangeRequirementStatus("archived")}
                      >
                        Archivar
                      </button>
                    ) : null}
                  </div>
                ) : null}

                {canEditRequirements && requirementEdit ? (
                  <div className="requirement-edit-box">
                    <h4>Editar necesidad</h4>
                    <div className="form-grid">
                      <label>
                        Proyecto
                        <select
                          value={requirementEdit.project_id}
                          onChange={(event) =>
                            onUpdateRequirementEdit({
                              project_id: event.target.value,
                            })
                          }
                        >
                          <option value="">Sin proyecto</option>
                          {editRequirementProjects.map((project) => (
                            <option key={project.id} value={project.id}>
                              {project.name}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label>
                        Título
                        <input
                          value={requirementEdit.title}
                          onChange={(event) =>
                            onUpdateRequirementEdit({
                              title: event.target.value,
                            })
                          }
                          type="text"
                        />
                      </label>
                      <label>
                        Prioridad
                        <select
                          value={requirementEdit.priority}
                          onChange={(event) =>
                            onUpdateRequirementEdit({
                              priority: event.target.value as RequirementEditState["priority"],
                            })
                          }
                        >
                          {REQUIREMENT_PRIORITIES.map((priority) => (
                            <option key={priority} value={priority}>
                              {formatRequirementPriority(priority)}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label>
                        Origen
                        <select
                          value={requirementEdit.source_type}
                          onChange={(event) =>
                            onUpdateRequirementEdit({
                              source_type:
                                event.target.value as RequirementEditState["source_type"],
                            })
                          }
                        >
                          {REQUIREMENT_SOURCE_TYPES.map((sourceType) => (
                            <option key={sourceType} value={sourceType}>
                              {formatRequirementSourceType(sourceType)}
                            </option>
                          ))}
                        </select>
                      </label>
                      {requirementTextFields.map((field) => (
                        <label key={field.key}>
                          {field.label}
                          <textarea
                            value={requirementEdit[field.key]}
                            onChange={(event) =>
                              onUpdateRequirementEdit({
                                [field.key]: event.target.value,
                              })
                            }
                            rows={field.rows ?? 2}
                          />
                          {field.helper ? (
                            <span className="field-helper">
                              {field.helper}
                            </span>
                          ) : null}
                        </label>
                      ))}
                    </div>
                    {requirementEditError ? (
                      <p className="error-message">{requirementEditError}</p>
                    ) : null}
                    <button
                      type="button"
                      disabled={isUpdatingRequirement}
                      onClick={onUpdateRequirement}
                    >
                      {isUpdatingRequirement ? "Guardando..." : "Guardar cambios"}
                    </button>
                  </div>
                ) : null}

                <div className="requirement-messages">
                  <h4>Discusión</h4>
                  <form
                    className="requirement-message-form"
                    onSubmit={onCreateRequirementMessage}
                  >
                    <label>
                      Tipo
                      <select
                        value={newRequirementMessageType}
                        onChange={(event) =>
                          onNewRequirementMessageTypeChange(
                            event.target.value as RequirementMessageType,
                          )
                        }
                      >
                        {REQUIREMENT_MESSAGE_TYPES.map((messageType) => (
                          <option key={messageType} value={messageType}>
                            {formatRequirementMessageType(messageType)}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      Mensaje
                      <textarea
                        value={newRequirementMessageBody}
                        onChange={(event) =>
                          onNewRequirementMessageBodyChange(event.target.value)
                        }
                        rows={3}
                      />
                    </label>
                    {requirementMessagesError ? (
                      <p className="error-message">
                        {requirementMessagesError}
                      </p>
                    ) : null}
                    <button type="submit" disabled={isCreatingRequirementMessage}>
                      {isCreatingRequirementMessage ? "Añadiendo..." : "Añadir mensaje"}
                    </button>
                  </form>

                  <ul className="message-timeline">
                    {requirementMessages.length > 0 ? (
                      requirementMessages.map((message) => (
                        <li key={message.id}>
                          <div>
                            <strong>
                              {formatRequirementMessageType(message.message_type)}
                            </strong>
                            <span>
                              {message.author
                                ? message.author.full_name
                                : "Usuario eliminado"}{" "}
                              - {formatDateTime(message.created_at)}
                            </span>
                          </div>
                          <p>{message.body}</p>
                        </li>
                      ))
                    ) : (
                      <li>
                        <p className="small-muted">Sin mensajes todavía.</p>
                      </li>
                    )}
                  </ul>
                </div>
              </>
            ) : (
              <p className="small-muted">
                Selecciona una necesidad para ver su detalle.
              </p>
            )}
          </div>
        </div>
      </div>

      {canCreateRequirements ? (
        <div className="admin-section">
          <form className="admin-form" onSubmit={onCreateRequirement}>
            <h3>Nueva necesidad</h3>
            <div className="form-grid">
              <label>
                Organización
                <select
                  value={newRequirement.organization_id}
                  onChange={(event) =>
                    onUpdateNewRequirement({
                      organization_id: event.target.value,
                      project_id: "",
                    })
                  }
                  required
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
                Proyecto
                <select
                  value={newRequirement.project_id}
                  onChange={(event) =>
                    onUpdateNewRequirement({ project_id: event.target.value })
                  }
                >
                  <option value="">Sin proyecto</option>
                  {newRequirementProjects.map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Título
                <input
                  value={newRequirement.title}
                  onChange={(event) =>
                    onUpdateNewRequirement({ title: event.target.value })
                  }
                  required
                  type="text"
                />
              </label>
              <label>
                Prioridad
                <select
                  value={newRequirement.priority}
                  onChange={(event) =>
                    onUpdateNewRequirement({
                      priority:
                        event.target.value as RequirementFormState["priority"],
                    })
                  }
                >
                  {REQUIREMENT_PRIORITIES.map((priority) => (
                    <option key={priority} value={priority}>
                      {formatRequirementPriority(priority)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Origen
                <select
                  value={newRequirement.source_type}
                  onChange={(event) =>
                    onUpdateNewRequirement({
                      source_type:
                        event.target.value as RequirementFormState["source_type"],
                    })
                  }
                >
                  {REQUIREMENT_SOURCE_TYPES.map((sourceType) => (
                    <option key={sourceType} value={sourceType}>
                      {formatRequirementSourceType(sourceType)}
                    </option>
                  ))}
                </select>
              </label>
              {requirementTextFields.map((field) => (
                <label key={field.key}>
                  {field.label}
                  <textarea
                    value={newRequirement[field.key]}
                    onChange={(event) =>
                      onUpdateNewRequirement({
                        [field.key]: event.target.value,
                      })
                    }
                    rows={field.rows ?? 2}
                  />
                  {field.helper ? (
                    <span className="field-helper">{field.helper}</span>
                  ) : null}
                </label>
              ))}
            </div>
            {requirementFormError ? (
              <p className="error-message">{requirementFormError}</p>
            ) : null}
            <button type="submit" disabled={isCreatingRequirement}>
              {isCreatingRequirement ? "Creando..." : "Crear necesidad"}
            </button>
          </form>
        </div>
      ) : null}
    </section>
  );
}
