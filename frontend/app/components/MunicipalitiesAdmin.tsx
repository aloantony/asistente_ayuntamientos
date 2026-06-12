import type { FormEvent } from "react";
import {
  formatMunicipalityStatus,
  formatMunicipalityType,
  formatRuralUrbanProfile,
  MUNICIPALITY_STATUSES,
  MUNICIPALITY_TYPES,
  RURAL_URBAN_PROFILES,
  userHasPermission,
  type Municipality,
  type MunicipalityEditState,
  type MunicipalityStatus,
  type MunicipalityType,
  type RuralUrbanProfile,
  type User,
} from "./types";

export type MunicipalitiesAdminProps = {
  currentUser: User;
  municipalities: Municipality[];
  isLoadingAdmin: boolean;
  municipalityPage: number;
  municipalityTotal: number;
  municipalityPageSize: number;
  municipalitySearchText: string;
  municipalityProvinceFilter: string;
  municipalityAutonomousCommunityFilter: string;
  municipalityStatusFilter: string;
  municipalityIncludeArchived: boolean;
  newMunicipality: MunicipalityEditState;
  municipalityFormError: string;
  isCreatingMunicipality: boolean;
  municipalityEdits: Record<number, MunicipalityEditState>;
  municipalityEditError: string;
  municipalityEditMessage: string;
  updatingMunicipalityId: number | null;
  onMunicipalitySearchTextChange: (searchText: string) => void;
  onMunicipalityProvinceFilterChange: (province: string) => void;
  onMunicipalityAutonomousCommunityFilterChange: (
    autonomousCommunity: string,
  ) => void;
  onMunicipalityStatusFilterChange: (status: string) => void;
  onMunicipalityIncludeArchivedChange: (includeArchived: boolean) => void;
  onMunicipalityPrevPage: () => void;
  onMunicipalityNextPage: () => void;
  onUpdateNewMunicipality: (updates: Partial<MunicipalityEditState>) => void;
  onCreateMunicipality: (event: FormEvent<HTMLFormElement>) => void;
  onUpdateMunicipalityEdit: (
    municipalityId: number,
    updates: Partial<MunicipalityEditState>,
  ) => void;
  onUpdateMunicipality: (municipalityId: number) => void;
  onArchiveMunicipality: (municipality: Municipality) => void;
};

export function MunicipalitiesAdmin({
  currentUser,
  municipalities,
  isLoadingAdmin,
  municipalityPage,
  municipalityTotal,
  municipalityPageSize,
  municipalitySearchText,
  municipalityProvinceFilter,
  municipalityAutonomousCommunityFilter,
  municipalityStatusFilter,
  municipalityIncludeArchived,
  newMunicipality,
  municipalityFormError,
  isCreatingMunicipality,
  municipalityEdits,
  municipalityEditError,
  municipalityEditMessage,
  updatingMunicipalityId,
  onMunicipalitySearchTextChange,
  onMunicipalityProvinceFilterChange,
  onMunicipalityAutonomousCommunityFilterChange,
  onMunicipalityStatusFilterChange,
  onMunicipalityIncludeArchivedChange,
  onMunicipalityPrevPage,
  onMunicipalityNextPage,
  onUpdateNewMunicipality,
  onCreateMunicipality,
  onUpdateMunicipalityEdit,
  onUpdateMunicipality,
  onArchiveMunicipality,
}: MunicipalitiesAdminProps) {
  const canView =
    userHasPermission(currentUser, "municipalities.view") ||
    userHasPermission(currentUser, "municipalities.manage");
  const canCreate =
    userHasPermission(currentUser, "municipalities.create") ||
    userHasPermission(currentUser, "municipalities.manage");
  const canEdit =
    userHasPermission(currentUser, "municipalities.edit") ||
    userHasPermission(currentUser, "municipalities.manage");
  const canArchive =
    userHasPermission(currentUser, "municipalities.archive") ||
    userHasPermission(currentUser, "municipalities.manage");

  const visibleMunicipalities = municipalities.filter((municipality) => {
    const search = municipalitySearchText.trim().toLowerCase();
    if (
      search &&
      ![
        municipality.name,
        municipality.province,
        municipality.autonomous_community,
        municipality.ine_code ?? "",
      ]
        .join(" ")
        .toLowerCase()
        .includes(search)
    ) {
      return false;
    }
    if (
      municipalityProvinceFilter &&
      municipality.province !== municipalityProvinceFilter
    ) {
      return false;
    }
    if (
      municipalityAutonomousCommunityFilter &&
      municipality.autonomous_community !==
        municipalityAutonomousCommunityFilter
    ) {
      return false;
    }
    if (
      municipalityStatusFilter &&
      municipality.status !== municipalityStatusFilter
    ) {
      return false;
    }
    return municipalityIncludeArchived || municipality.status !== "archived";
  });
  const municipalityPageCount = Math.max(
    1,
    Math.ceil(municipalityTotal / municipalityPageSize),
  );
  const provinceOptions = Array.from(
    new Set(municipalities.map((municipality) => municipality.province)),
  ).sort();
  const autonomousCommunityOptions = Array.from(
    new Set(
      municipalities.map((municipality) => municipality.autonomous_community),
    ),
  ).sort();

  return (
    <div className="admin-section">
      <div className="section-header">
        <h3>Municipios</h3>
        {isLoadingAdmin ? (
          <p className="small-muted">Cargando municipios.</p>
        ) : null}
      </div>

      {canView ? (
        <>
          <div className="municipality-filters">
            <label>
              Buscar
              <input
                onChange={(event) =>
                  onMunicipalitySearchTextChange(event.target.value)
                }
                type="search"
                value={municipalitySearchText}
              />
            </label>

            <label>
              Provincia
              <select
                onChange={(event) =>
                  onMunicipalityProvinceFilterChange(event.target.value)
                }
                value={municipalityProvinceFilter}
              >
                <option value="">Todas</option>
                {provinceOptions.map((province) => (
                  <option key={province} value={province}>
                    {province}
                  </option>
                ))}
              </select>
            </label>

            <label>
              Comunidad autónoma
              <select
                onChange={(event) =>
                  onMunicipalityAutonomousCommunityFilterChange(
                    event.target.value,
                  )
                }
                value={municipalityAutonomousCommunityFilter}
              >
                <option value="">Todas</option>
                {autonomousCommunityOptions.map((autonomousCommunity) => (
                  <option key={autonomousCommunity} value={autonomousCommunity}>
                    {autonomousCommunity}
                  </option>
                ))}
              </select>
            </label>

            <label>
              Estado
              <select
                onChange={(event) =>
                  onMunicipalityStatusFilterChange(event.target.value)
                }
                value={municipalityStatusFilter}
              >
                <option value="">Todos</option>
                {MUNICIPALITY_STATUSES.map((status) => (
                  <option key={status} value={status}>
                    {formatMunicipalityStatus(status)}
                  </option>
                ))}
              </select>
            </label>

            <label className="checkbox-label municipalities-archive-toggle">
              <input
                checked={municipalityIncludeArchived}
                onChange={(event) =>
                  onMunicipalityIncludeArchivedChange(event.target.checked)
                }
                type="checkbox"
              />
              Incluir archivados
            </label>
          </div>

          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Nombre</th>
                  <th>Provincia</th>
                  <th>Comunidad autónoma</th>
                  <th>País</th>
                  <th>INE</th>
                  <th>Población</th>
                  <th>Superficie</th>
                  <th>Densidad</th>
                  <th>Códigos postales</th>
                  <th>Tipo</th>
                  <th>Perfil rural/urbano</th>
                  <th>Perfil económico</th>
                  <th>Perfil turístico</th>
                  <th>Notas geográficas</th>
                  <th>Notas administrativas</th>
                  <th>Estado</th>
                  <th>Acción</th>
                </tr>
              </thead>
              <tbody>
                {visibleMunicipalities.length > 0 ? (
                  visibleMunicipalities.map((municipality) => {
                    const edit = municipalityEdits[municipality.id] ?? {
                      name: municipality.name,
                      province: municipality.province,
                      autonomous_community:
                        municipality.autonomous_community,
                      country: municipality.country,
                      ine_code: municipality.ine_code ?? "",
                      population:
                        municipality.population === null
                          ? ""
                          : String(municipality.population),
                      surface_km2:
                        municipality.surface_km2 === null
                          ? ""
                          : String(municipality.surface_km2),
                      density:
                        municipality.density === null
                          ? ""
                          : String(municipality.density),
                      postal_codes: municipality.postal_codes ?? "",
                      municipality_type: municipality.municipality_type,
                      rural_urban_profile: municipality.rural_urban_profile,
                      economic_profile: municipality.economic_profile ?? "",
                      tourism_profile: municipality.tourism_profile ?? "",
                      geographic_notes: municipality.geographic_notes ?? "",
                      administrative_notes:
                        municipality.administrative_notes ?? "",
                      status: municipality.status,
                    };

                    return (
                      <tr key={municipality.id}>
                        <td>{municipality.id}</td>
                        <EditableTextCell
                          canEdit={canEdit}
                          label={`Nombre de ${municipality.name}`}
                          value={edit.name}
                          onChange={(value) =>
                            onUpdateMunicipalityEdit(municipality.id, {
                              name: value,
                            })
                          }
                        />
                        <EditableTextCell
                          canEdit={canEdit}
                          label={`Provincia de ${municipality.name}`}
                          value={edit.province}
                          onChange={(value) =>
                            onUpdateMunicipalityEdit(municipality.id, {
                              province: value,
                            })
                          }
                        />
                        <EditableTextCell
                          canEdit={canEdit}
                          label={`Comunidad autónoma de ${municipality.name}`}
                          value={edit.autonomous_community}
                          onChange={(value) =>
                            onUpdateMunicipalityEdit(municipality.id, {
                              autonomous_community: value,
                            })
                          }
                        />
                        <EditableTextCell
                          canEdit={canEdit}
                          label={`País de ${municipality.name}`}
                          value={edit.country}
                          onChange={(value) =>
                            onUpdateMunicipalityEdit(municipality.id, {
                              country: value,
                            })
                          }
                        />
                        <EditableTextCell
                          canEdit={canEdit}
                          label={`Código INE de ${municipality.name}`}
                          value={edit.ine_code}
                          onChange={(value) =>
                            onUpdateMunicipalityEdit(municipality.id, {
                              ine_code: value,
                            })
                          }
                        />
                        <EditableTextCell
                          canEdit={canEdit}
                          inputMode="numeric"
                          label={`Población de ${municipality.name}`}
                          type="number"
                          value={edit.population}
                          onChange={(value) =>
                            onUpdateMunicipalityEdit(municipality.id, {
                              population: value,
                            })
                          }
                        />
                        <EditableTextCell
                          canEdit={canEdit}
                          inputMode="decimal"
                          label={`Superficie de ${municipality.name}`}
                          step="0.01"
                          type="number"
                          value={edit.surface_km2}
                          onChange={(value) =>
                            onUpdateMunicipalityEdit(municipality.id, {
                              surface_km2: value,
                            })
                          }
                        />
                        <EditableTextCell
                          canEdit={canEdit}
                          inputMode="decimal"
                          label={`Densidad de ${municipality.name}`}
                          step="0.01"
                          type="number"
                          value={edit.density}
                          onChange={(value) =>
                            onUpdateMunicipalityEdit(municipality.id, {
                              density: value,
                            })
                          }
                        />
                        <EditableTextareaCell
                          canEdit={canEdit}
                          label={`Códigos postales de ${municipality.name}`}
                          value={edit.postal_codes}
                          onChange={(value) =>
                            onUpdateMunicipalityEdit(municipality.id, {
                              postal_codes: value,
                            })
                          }
                        />
                        <td>
                          {canEdit ? (
                            <select
                              aria-label={`Tipo de ${municipality.name}`}
                              className="table-input"
                              onChange={(event) =>
                                onUpdateMunicipalityEdit(municipality.id, {
                                  municipality_type: event.target
                                    .value as MunicipalityType,
                                })
                              }
                              value={edit.municipality_type}
                            >
                              {MUNICIPALITY_TYPES.map((municipalityType) => (
                                <option
                                  key={municipalityType}
                                  value={municipalityType}
                                >
                                  {formatMunicipalityType(municipalityType)}
                                </option>
                              ))}
                            </select>
                          ) : (
                            formatMunicipalityType(municipality.municipality_type)
                          )}
                        </td>
                        <td>
                          {canEdit ? (
                            <select
                              aria-label={`Perfil rural urbano de ${municipality.name}`}
                              className="table-input"
                              onChange={(event) =>
                                onUpdateMunicipalityEdit(municipality.id, {
                                  rural_urban_profile: event.target
                                    .value as RuralUrbanProfile,
                                })
                              }
                              value={edit.rural_urban_profile}
                            >
                              {RURAL_URBAN_PROFILES.map((profile) => (
                                <option key={profile} value={profile}>
                                  {formatRuralUrbanProfile(profile)}
                                </option>
                              ))}
                            </select>
                          ) : (
                            formatRuralUrbanProfile(
                              municipality.rural_urban_profile,
                            )
                          )}
                        </td>
                        <EditableTextareaCell
                          canEdit={canEdit}
                          label={`Perfil económico de ${municipality.name}`}
                          value={edit.economic_profile}
                          onChange={(value) =>
                            onUpdateMunicipalityEdit(municipality.id, {
                              economic_profile: value,
                            })
                          }
                        />
                        <EditableTextareaCell
                          canEdit={canEdit}
                          label={`Perfil turístico de ${municipality.name}`}
                          value={edit.tourism_profile}
                          onChange={(value) =>
                            onUpdateMunicipalityEdit(municipality.id, {
                              tourism_profile: value,
                            })
                          }
                        />
                        <EditableTextareaCell
                          canEdit={canEdit}
                          label={`Notas geográficas de ${municipality.name}`}
                          value={edit.geographic_notes}
                          onChange={(value) =>
                            onUpdateMunicipalityEdit(municipality.id, {
                              geographic_notes: value,
                            })
                          }
                        />
                        <EditableTextareaCell
                          canEdit={canEdit}
                          label={`Notas administrativas de ${municipality.name}`}
                          value={edit.administrative_notes}
                          onChange={(value) =>
                            onUpdateMunicipalityEdit(municipality.id, {
                              administrative_notes: value,
                            })
                          }
                        />
                        <td>
                          {canEdit ? (
                            <select
                              aria-label={`Estado de ${municipality.name}`}
                              className="table-input"
                              onChange={(event) =>
                                onUpdateMunicipalityEdit(municipality.id, {
                                  status: event.target
                                    .value as MunicipalityStatus,
                                })
                              }
                              value={edit.status}
                            >
                              {MUNICIPALITY_STATUSES.map((status) => (
                                <option key={status} value={status}>
                                  {formatMunicipalityStatus(status)}
                                </option>
                              ))}
                            </select>
                          ) : (
                            formatMunicipalityStatus(municipality.status)
                          )}
                        </td>
                        <td>
                          <div className="table-actions">
                            {canEdit ? (
                              <button
                                type="button"
                                onClick={() =>
                                  onUpdateMunicipality(municipality.id)
                                }
                                disabled={
                                  updatingMunicipalityId === municipality.id ||
                                  isLoadingAdmin
                                }
                              >
                                {updatingMunicipalityId === municipality.id
                                  ? "Guardando..."
                                  : "Guardar"}
                              </button>
                            ) : null}
                            {canArchive ? (
                              <button
                                className="danger-button"
                                type="button"
                                onClick={() =>
                                  onArchiveMunicipality(municipality)
                                }
                                disabled={
                                  municipality.status === "archived" ||
                                  updatingMunicipalityId === municipality.id ||
                                  isLoadingAdmin
                                }
                              >
                                Archivar
                              </button>
                            ) : null}
                          </div>
                        </td>
                      </tr>
                    );
                  })
                ) : (
                  <tr>
                    <td colSpan={18}>No hay municipios para mostrar.</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="pager-row">
            <button
              className="secondary-button"
              type="button"
              onClick={onMunicipalityPrevPage}
              disabled={municipalityPage === 0 || isLoadingAdmin}
            >
              Anterior
            </button>
            <span className="pager-status">
              Página {municipalityPage + 1} de {municipalityPageCount} (
              {municipalityTotal} en total)
            </span>
            <button
              className="secondary-button"
              type="button"
              onClick={onMunicipalityNextPage}
              disabled={
                municipalityPage + 1 >= municipalityPageCount || isLoadingAdmin
              }
            >
              Siguiente
            </button>
          </div>
        </>
      ) : (
        <p className="small-muted">No tienes permisos para listar municipios.</p>
      )}

      {municipalityEditError ? (
        <p className="error-message">{municipalityEditError}</p>
      ) : null}
      {municipalityEditMessage ? (
        <p className="success-message">{municipalityEditMessage}</p>
      ) : null}

      {canCreate ? (
        <form className="admin-form" onSubmit={onCreateMunicipality}>
          <h4>Crear municipio</h4>
          <div className="form-grid">
            <MunicipalityFormFields
              edit={newMunicipality}
              onUpdate={onUpdateNewMunicipality}
            />
          </div>

          {municipalityFormError ? (
            <p className="error-message">{municipalityFormError}</p>
          ) : null}

          <button type="submit" disabled={isCreatingMunicipality}>
            {isCreatingMunicipality ? "Creando..." : "Crear municipio"}
          </button>
        </form>
      ) : null}
    </div>
  );
}

function EditableTextCell({
  canEdit,
  inputMode,
  label,
  step,
  type = "text",
  value,
  onChange,
}: {
  canEdit: boolean;
  inputMode?: "decimal" | "numeric";
  label: string;
  step?: string;
  type?: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <td>
      {canEdit ? (
        <input
          aria-label={label}
          className="table-input"
          inputMode={inputMode}
          min={type === "number" ? "0" : undefined}
          onChange={(event) => onChange(event.target.value)}
          step={step}
          type={type}
          value={value}
        />
      ) : (
        value || <span className="small-muted">Sin dato</span>
      )}
    </td>
  );
}

function EditableTextareaCell({
  canEdit,
  label,
  value,
  onChange,
}: {
  canEdit: boolean;
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <td>
      {canEdit ? (
        <textarea
          aria-label={label}
          className="table-textarea"
          onChange={(event) => onChange(event.target.value)}
          rows={2}
          value={value}
        />
      ) : (
        value || <span className="small-muted">Sin dato</span>
      )}
    </td>
  );
}

function MunicipalityFormFields({
  edit,
  onUpdate,
}: {
  edit: MunicipalityEditState;
  onUpdate: (updates: Partial<MunicipalityEditState>) => void;
}) {
  return (
    <>
      <label>
        Nombre
        <input
          onChange={(event) => onUpdate({ name: event.target.value })}
          required
          type="text"
          value={edit.name}
        />
      </label>

      <label>
        Provincia
        <input
          onChange={(event) => onUpdate({ province: event.target.value })}
          required
          type="text"
          value={edit.province}
        />
      </label>

      <label>
        Comunidad autónoma
        <input
          onChange={(event) =>
            onUpdate({ autonomous_community: event.target.value })
          }
          required
          type="text"
          value={edit.autonomous_community}
        />
      </label>

      <label>
        País
        <input
          onChange={(event) => onUpdate({ country: event.target.value })}
          required
          type="text"
          value={edit.country}
        />
      </label>

      <label>
        Código INE
        <input
          onChange={(event) => onUpdate({ ine_code: event.target.value })}
          type="text"
          value={edit.ine_code}
        />
      </label>

      <label>
        Población
        <input
          min="0"
          onChange={(event) => onUpdate({ population: event.target.value })}
          step="1"
          type="number"
          value={edit.population}
        />
      </label>

      <label>
        Superficie km2
        <input
          min="0"
          onChange={(event) => onUpdate({ surface_km2: event.target.value })}
          step="0.01"
          type="number"
          value={edit.surface_km2}
        />
      </label>

      <label>
        Densidad
        <input
          min="0"
          onChange={(event) => onUpdate({ density: event.target.value })}
          step="0.01"
          type="number"
          value={edit.density}
        />
      </label>

      <label>
        Códigos postales
        <textarea
          onChange={(event) => onUpdate({ postal_codes: event.target.value })}
          rows={3}
          value={edit.postal_codes}
        />
      </label>

      <label>
        Tipo
        <select
          onChange={(event) =>
            onUpdate({
              municipality_type: event.target.value as MunicipalityType,
            })
          }
          value={edit.municipality_type}
        >
          {MUNICIPALITY_TYPES.map((municipalityType) => (
            <option key={municipalityType} value={municipalityType}>
              {formatMunicipalityType(municipalityType)}
            </option>
          ))}
        </select>
      </label>

      <label>
        Perfil rural/urbano
        <select
          onChange={(event) =>
            onUpdate({
              rural_urban_profile: event.target.value as RuralUrbanProfile,
            })
          }
          value={edit.rural_urban_profile}
        >
          {RURAL_URBAN_PROFILES.map((profile) => (
            <option key={profile} value={profile}>
              {formatRuralUrbanProfile(profile)}
            </option>
          ))}
        </select>
      </label>

      <label>
        Estado
        <select
          onChange={(event) =>
            onUpdate({ status: event.target.value as MunicipalityStatus })
          }
          value={edit.status}
        >
          {MUNICIPALITY_STATUSES.map((status) => (
            <option key={status} value={status}>
              {formatMunicipalityStatus(status)}
            </option>
          ))}
        </select>
      </label>

      <label>
        Perfil económico
        <textarea
          onChange={(event) =>
            onUpdate({ economic_profile: event.target.value })
          }
          rows={3}
          value={edit.economic_profile}
        />
      </label>

      <label>
        Perfil turístico
        <textarea
          onChange={(event) => onUpdate({ tourism_profile: event.target.value })}
          rows={3}
          value={edit.tourism_profile}
        />
      </label>

      <label>
        Notas geográficas
        <textarea
          onChange={(event) =>
            onUpdate({ geographic_notes: event.target.value })
          }
          rows={3}
          value={edit.geographic_notes}
        />
      </label>

      <label>
        Notas administrativas
        <textarea
          onChange={(event) =>
            onUpdate({ administrative_notes: event.target.value })
          }
          rows={3}
          value={edit.administrative_notes}
        />
      </label>
    </>
  );
}
