import type { FormEvent } from "react";
import {
  formatMunicipalityOption,
  formatOrganizationStatus,
  formatUserOption,
  ORGANIZATION_STATUSES,
  type MembershipAction,
  type Municipality,
  type Organization,
  type OrganizationEditState,
  type OrganizationStatus,
  type User,
} from "./types";

export type OrganizationsAdminProps = {
  organizations: Organization[];
  municipalities: Municipality[];
  adminUsers: User[];
  isLoadingAdmin: boolean;
  newOrganizationName: string;
  newOrganizationDescription: string;
  newOrganizationMunicipalityId: string;
  newOrganizationStatus: OrganizationStatus;
  organizationFormError: string;
  isCreatingOrganization: boolean;
  organizationEdits: Record<number, OrganizationEditState>;
  organizationEditError: string;
  organizationEditMessage: string;
  updatingOrganizationId: number | null;
  organizationMembershipOrganizationId: string;
  organizationMembershipUserId: string;
  organizationMembershipError: string;
  organizationMembershipMessage: string;
  isUpdatingOrganizationMembership: boolean;
  onNewOrganizationNameChange: (name: string) => void;
  onNewOrganizationDescriptionChange: (description: string) => void;
  onNewOrganizationMunicipalityIdChange: (municipalityId: string) => void;
  onNewOrganizationStatusChange: (status: OrganizationStatus) => void;
  onCreateOrganization: (event: FormEvent<HTMLFormElement>) => void;
  onUpdateOrganizationEdit: (
    organizationId: number,
    updates: Partial<OrganizationEditState>,
  ) => void;
  onUpdateOrganization: (organizationId: number) => void;
  onOrganizationMembershipOrganizationIdChange: (organizationId: string) => void;
  onOrganizationMembershipUserIdChange: (userId: string) => void;
  onUpdateOrganizationMembership: (action: MembershipAction) => void;
};

export function OrganizationsAdmin({
  organizations,
  municipalities,
  adminUsers,
  isLoadingAdmin,
  newOrganizationName,
  newOrganizationDescription,
  newOrganizationMunicipalityId,
  newOrganizationStatus,
  organizationFormError,
  isCreatingOrganization,
  organizationEdits,
  organizationEditError,
  organizationEditMessage,
  updatingOrganizationId,
  organizationMembershipOrganizationId,
  organizationMembershipUserId,
  organizationMembershipError,
  organizationMembershipMessage,
  isUpdatingOrganizationMembership,
  onNewOrganizationNameChange,
  onNewOrganizationDescriptionChange,
  onNewOrganizationMunicipalityIdChange,
  onNewOrganizationStatusChange,
  onCreateOrganization,
  onUpdateOrganizationEdit,
  onUpdateOrganization,
  onOrganizationMembershipOrganizationIdChange,
  onOrganizationMembershipUserIdChange,
  onUpdateOrganizationMembership,
}: OrganizationsAdminProps) {
  const activeMunicipalities = municipalities.filter(
    (municipality) => municipality.status === "active",
  );

  return (
    <div className="admin-section">
      <div className="section-header">
        <h3>Organizaciones</h3>
        {isLoadingAdmin ? (
          <p className="small-muted">Cargando organizaciones.</p>
        ) : null}
      </div>

      <div className="table-wrapper">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Nombre</th>
              <th>Descripción</th>
              <th>Municipio vinculado</th>
              <th>Estado</th>
              <th>Usuarios</th>
              <th>Acción</th>
            </tr>
          </thead>
          <tbody>
            {organizations.length > 0 ? (
              organizations.map((organization) => {
                const edit = organizationEdits[organization.id] ?? {
                  name: organization.name,
                  description: organization.description ?? "",
                  municipality_id:
                    organization.municipality_id === null
                      ? ""
                      : String(organization.municipality_id),
                  status: organization.status,
                };
                const selectableMunicipalities = organization.municipality
                  ? [
                      organization.municipality,
                      ...activeMunicipalities.filter(
                        (municipality) =>
                          municipality.id !== organization.municipality?.id,
                      ),
                    ]
                  : activeMunicipalities;

                return (
                  <tr key={organization.id}>
                    <td>{organization.id}</td>
                    <td>
                      <input
                        aria-label={`Nombre de la organización ${organization.name}`}
                        className="table-input"
                        onChange={(event) =>
                          onUpdateOrganizationEdit(organization.id, {
                            name: event.target.value,
                          })
                        }
                        type="text"
                        value={edit.name}
                      />
                    </td>
                    <td>
                      {selectableMunicipalities.length > 0 ? (
                        <select
                          aria-label={`Municipio vinculado a ${organization.name}`}
                          className="table-input"
                          onChange={(event) =>
                            onUpdateOrganizationEdit(organization.id, {
                              municipality_id: event.target.value,
                            })
                          }
                          value={edit.municipality_id}
                        >
                          <option value="">Sin municipio vinculado</option>
                          {selectableMunicipalities.map((municipality) => (
                            <option key={municipality.id} value={municipality.id}>
                              {formatMunicipalityOption(municipality)}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <span className="small-muted">
                          Sin municipios disponibles
                        </span>
                      )}
                      {organization.municipality ? (
                        <span className="small-muted">
                          {organization.municipality.autonomous_community}
                        </span>
                      ) : null}
                    </td>
                    <td>
                      <textarea
                        aria-label={`Descripción de la organización ${organization.name}`}
                        className="table-textarea"
                        onChange={(event) =>
                          onUpdateOrganizationEdit(organization.id, {
                            description: event.target.value,
                          })
                        }
                        rows={2}
                        value={edit.description}
                      />
                    </td>
                    <td>
                      <select
                        aria-label={`Estado de la organización ${organization.name}`}
                        className="table-input"
                        onChange={(event) =>
                          onUpdateOrganizationEdit(organization.id, {
                            status: event.target.value as OrganizationStatus,
                          })
                        }
                        value={edit.status}
                      >
                        {ORGANIZATION_STATUSES.map((status) => (
                          <option key={status} value={status}>
                            {formatOrganizationStatus(status)}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td>
                      {organization.users.length > 0 ? (
                        <ul className="compact-list">
                          {organization.users.map((organizationUser) => (
                            <li key={organizationUser.id}>
                              <strong>{organizationUser.full_name}</strong>
                              <span>{organizationUser.email}</span>
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <span className="small-muted">Sin usuarios</span>
                      )}
                    </td>
                    <td>
                      <button
                        type="button"
                        onClick={() => onUpdateOrganization(organization.id)}
                        disabled={
                          updatingOrganizationId === organization.id ||
                          isLoadingAdmin
                        }
                      >
                        {updatingOrganizationId === organization.id
                          ? "Guardando..."
                          : "Guardar"}
                      </button>
                    </td>
                  </tr>
                );
              })
            ) : (
              <tr>
                <td colSpan={7}>No hay organizaciones para mostrar.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {organizationEditError ? (
        <p className="error-message">{organizationEditError}</p>
      ) : null}
      {organizationEditMessage ? (
        <p className="success-message">{organizationEditMessage}</p>
      ) : null}

      <form className="admin-form" onSubmit={onCreateOrganization}>
        <h4>Crear organización</h4>
        <div className="form-grid">
          <label>
            Nombre
            <input
              name="new-organization-name"
              onChange={(event) =>
                onNewOrganizationNameChange(event.target.value)
              }
              required
              type="text"
              value={newOrganizationName}
            />
          </label>

          <label>
            Estado
            <select
              name="new-organization-status"
              onChange={(event) =>
                onNewOrganizationStatusChange(
                  event.target.value as OrganizationStatus,
                )
              }
              value={newOrganizationStatus}
            >
              {ORGANIZATION_STATUSES.map((status) => (
                <option key={status} value={status}>
                  {formatOrganizationStatus(status)}
                </option>
              ))}
            </select>
          </label>

          <label>
            Municipio vinculado
            <select
              name="new-organization-municipality"
              onChange={(event) =>
                onNewOrganizationMunicipalityIdChange(event.target.value)
              }
              value={newOrganizationMunicipalityId}
            >
              <option value="">Sin municipio vinculado</option>
              {activeMunicipalities.map((municipality) => (
                <option key={municipality.id} value={municipality.id}>
                  {formatMunicipalityOption(municipality)}
                </option>
              ))}
            </select>
          </label>

          <label>
            Descripción
            <textarea
              name="new-organization-description"
              onChange={(event) =>
                onNewOrganizationDescriptionChange(event.target.value)
              }
              rows={3}
              value={newOrganizationDescription}
            />
          </label>
        </div>

        {organizationFormError ? (
          <p className="error-message">{organizationFormError}</p>
        ) : null}

        <button type="submit" disabled={isCreatingOrganization}>
          {isCreatingOrganization ? "Creando..." : "Crear organización"}
        </button>
      </form>

      <div>
        <h4>Usuarios de organización</h4>
        <div className="membership-controls">
          <label>
            Organización
            <select
              onChange={(event) =>
                onOrganizationMembershipOrganizationIdChange(event.target.value)
              }
              value={organizationMembershipOrganizationId}
            >
              <option value="">Selecciona una organización</option>
              {organizations.map((organization) => (
                <option key={organization.id} value={organization.id}>
                  {organization.name}
                </option>
              ))}
            </select>
          </label>

          <label>
            Usuario
            <select
              onChange={(event) =>
                onOrganizationMembershipUserIdChange(event.target.value)
              }
              value={organizationMembershipUserId}
            >
              <option value="">Selecciona un usuario</option>
              {adminUsers.map((adminUser) => (
                <option key={adminUser.id} value={adminUser.id}>
                  {formatUserOption(adminUser)}
                </option>
              ))}
            </select>
          </label>
        </div>

        {organizationMembershipError ? (
          <p className="error-message">{organizationMembershipError}</p>
        ) : null}
        {organizationMembershipMessage ? (
          <p className="success-message">{organizationMembershipMessage}</p>
        ) : null}

        <div className="button-row">
          <button
            type="button"
            onClick={() => onUpdateOrganizationMembership("add")}
            disabled={isUpdatingOrganizationMembership}
          >
            Añadir usuario
          </button>
          <button
            className="secondary-button"
            type="button"
            onClick={() => onUpdateOrganizationMembership("remove")}
            disabled={isUpdatingOrganizationMembership}
          >
            Quitar usuario
          </button>
        </div>
      </div>
    </div>
  );
}
