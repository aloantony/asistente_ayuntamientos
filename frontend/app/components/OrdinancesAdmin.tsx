"use client";

import Link from "next/link";
import { useEffect, useState, type FormEvent } from "react";
import {
  formatMunicipalityOption,
  formatOrdinanceCurationStatus,
  formatOrdinanceStatus,
  formatOrdinanceType,
  ORDINANCE_CURATION_STATUSES,
  ORDINANCE_STATUSES,
  ORDINANCE_TYPES,
  userHasPermission,
  type Municipality,
  type Ordinance,
  type OrdinanceCurationStatus,
  type OrdinanceEditState,
  type OrdinanceStatus,
  type OrdinanceType,
  type User,
} from "./types";

export type OrdinanceFilterValues = {
  q: string;
  province: string;
  autonomousCommunity: string;
  topic: string;
  status: string;
  includeArchived: boolean;
};

const EMPTY_ORDINANCE_FILTERS: OrdinanceFilterValues = {
  q: "",
  province: "",
  autonomousCommunity: "",
  topic: "",
  status: "",
  includeArchived: false,
};

export type OrdinancesAdminProps = {
  currentUser: User;
  municipalities: Municipality[];
  ordinances: Ordinance[];
  isLoadingAdmin: boolean;
  ordinancePage: number;
  ordinanceTotal: number;
  ordinancePageSize: number;
  ordinanceSearchText: string;
  ordinanceProvinceFilter: string;
  ordinanceAutonomousCommunityFilter: string;
  ordinanceTopicFilter: string;
  ordinanceStatusFilter: string;
  ordinanceIncludeArchived: boolean;
  newOrdinance: OrdinanceEditState;
  ordinanceFormError: string;
  isCreatingOrdinance: boolean;
  ordinanceEdits: Record<number, OrdinanceEditState>;
  ordinanceEditError: string;
  ordinanceEditMessage: string;
  updatingOrdinanceId: number | null;
  ordinanceDetailLoaded: Record<number, boolean>;
  loadingOrdinanceDetailId: number | null;
  onApplyOrdinanceFilters: (filters: OrdinanceFilterValues) => void;
  onOrdinancePrevPage: () => void;
  onOrdinanceNextPage: () => void;
  onUpdateNewOrdinance: (updates: Partial<OrdinanceEditState>) => void;
  onCreateOrdinance: (event: FormEvent<HTMLFormElement>) => void;
  onStartOrdinanceEdit: (ordinanceId: number) => void;
  onUpdateOrdinanceEdit: (
    ordinanceId: number,
    updates: Partial<OrdinanceEditState>,
  ) => void;
  onUpdateOrdinance: (ordinanceId: number) => void;
  onArchiveOrdinance: (ordinance: Ordinance) => void;
};

export function OrdinancesAdmin({
  currentUser,
  municipalities,
  ordinances,
  isLoadingAdmin,
  ordinancePage,
  ordinanceTotal,
  ordinancePageSize,
  ordinanceSearchText,
  ordinanceProvinceFilter,
  ordinanceAutonomousCommunityFilter,
  ordinanceTopicFilter,
  ordinanceStatusFilter,
  ordinanceIncludeArchived,
  newOrdinance,
  ordinanceFormError,
  isCreatingOrdinance,
  ordinanceEdits,
  ordinanceEditError,
  ordinanceEditMessage,
  updatingOrdinanceId,
  ordinanceDetailLoaded,
  loadingOrdinanceDetailId,
  onApplyOrdinanceFilters,
  onOrdinancePrevPage,
  onOrdinanceNextPage,
  onUpdateNewOrdinance,
  onCreateOrdinance,
  onStartOrdinanceEdit,
  onUpdateOrdinanceEdit,
  onUpdateOrdinance,
  onArchiveOrdinance,
}: OrdinancesAdminProps) {
  // Borrador local de filtros: se inicializa desde la URL (props aplicadas)
  // y solo viaja a la URL al enviar el formulario "Aplicar filtros".
  const [filterDraft, setFilterDraft] = useState<OrdinanceFilterValues>({
    q: ordinanceSearchText,
    province: ordinanceProvinceFilter,
    autonomousCommunity: ordinanceAutonomousCommunityFilter,
    topic: ordinanceTopicFilter,
    status: ordinanceStatusFilter,
    includeArchived: ordinanceIncludeArchived,
  });

  useEffect(() => {
    // Re-sincroniza los inputs cuando cambian los filtros aplicados en la URL
    // (atrás/adelante del navegador, enlaces profundos…).
    setFilterDraft({
      q: ordinanceSearchText,
      province: ordinanceProvinceFilter,
      autonomousCommunity: ordinanceAutonomousCommunityFilter,
      topic: ordinanceTopicFilter,
      status: ordinanceStatusFilter,
      includeArchived: ordinanceIncludeArchived,
    });
  }, [
    ordinanceSearchText,
    ordinanceProvinceFilter,
    ordinanceAutonomousCommunityFilter,
    ordinanceTopicFilter,
    ordinanceStatusFilter,
    ordinanceIncludeArchived,
  ]);

  function updateFilterDraft(updates: Partial<OrdinanceFilterValues>) {
    setFilterDraft((current) => ({ ...current, ...updates }));
  }

  function handleSubmitFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onApplyOrdinanceFilters(filterDraft);
  }

  function handleClearFilters() {
    setFilterDraft(EMPTY_ORDINANCE_FILTERS);
    onApplyOrdinanceFilters(EMPTY_ORDINANCE_FILTERS);
  }

  const canView =
    userHasPermission(currentUser, "ordinances.view") ||
    userHasPermission(currentUser, "ordinances.manage");
  const canCreate =
    userHasPermission(currentUser, "ordinances.create") ||
    userHasPermission(currentUser, "ordinances.manage");
  const canEdit =
    userHasPermission(currentUser, "ordinances.edit") ||
    userHasPermission(currentUser, "ordinances.manage");
  const canArchive =
    userHasPermission(currentUser, "ordinances.archive") ||
    userHasPermission(currentUser, "ordinances.manage");
  const canReview =
    userHasPermission(currentUser, "ordinances.review") ||
    userHasPermission(currentUser, "ordinances.manage");

  // La lista renderiza exactamente lo que devolvió el servidor; el filtrado
  // se hace con los parámetros de consulta del backend.
  const ordinancePageCount = Math.max(
    1,
    Math.ceil(ordinanceTotal / ordinancePageSize),
  );
  const municipalityOptions = getMunicipalityOptions(municipalities, ordinances);
  const activeMunicipalityOptions =
    municipalities.length > 0
      ? municipalities.filter((municipality) => municipality.status === "active")
      : municipalityOptions;
  // Sugerencias (datalist) con los valores distintos de los datos cargados;
  // al ser texto libre se puede escribir cualquier provincia/comunidad/tema y
  // el backend filtra sin distinguir mayúsculas.
  const provinceOptions = Array.from(
    new Set(municipalityOptions.map((municipality) => municipality.province)),
  ).sort();
  const autonomousCommunityOptions = Array.from(
    new Set(
      municipalityOptions.map(
        (municipality) => municipality.autonomous_community,
      ),
    ),
  ).sort();
  const topicOptions = Array.from(
    new Set(ordinances.map((ordinance) => ordinance.topic).filter(Boolean)),
  ).sort();

  return (
    <div className="admin-section">
      <div className="section-header">
        <h3>Ordenanzas</h3>
        {isLoadingAdmin ? (
          <p className="small-muted">Cargando ordenanzas.</p>
        ) : null}
      </div>

      {canView ? (
        <>
          <form className="ordinance-filters" onSubmit={handleSubmitFilters}>
            <label>
              Buscar
              <input
                onChange={(event) =>
                  updateFilterDraft({ q: event.target.value })
                }
                type="search"
                value={filterDraft.q}
              />
            </label>

            <label>
              Provincia
              <input
                list="ordinance-province-options"
                onChange={(event) =>
                  updateFilterDraft({ province: event.target.value })
                }
                type="text"
                value={filterDraft.province}
              />
              <datalist id="ordinance-province-options">
                {provinceOptions.map((province) => (
                  <option key={province} value={province} />
                ))}
              </datalist>
            </label>

            <label>
              Comunidad autónoma
              <input
                list="ordinance-community-options"
                onChange={(event) =>
                  updateFilterDraft({
                    autonomousCommunity: event.target.value,
                  })
                }
                type="text"
                value={filterDraft.autonomousCommunity}
              />
              <datalist id="ordinance-community-options">
                {autonomousCommunityOptions.map((autonomousCommunity) => (
                  <option
                    key={autonomousCommunity}
                    value={autonomousCommunity}
                  />
                ))}
              </datalist>
            </label>

            <label>
              Tema
              <input
                list="ordinance-topic-options"
                onChange={(event) =>
                  updateFilterDraft({ topic: event.target.value })
                }
                type="text"
                value={filterDraft.topic}
              />
              <datalist id="ordinance-topic-options">
                {topicOptions.map((topic) => (
                  <option key={topic} value={topic} />
                ))}
              </datalist>
            </label>

            <label>
              Estado
              <select
                onChange={(event) =>
                  updateFilterDraft({ status: event.target.value })
                }
                value={filterDraft.status}
              >
                <option value="">Todos</option>
                {ORDINANCE_STATUSES.map((status) => (
                  <option key={status} value={status}>
                    {formatOrdinanceStatus(status)}
                  </option>
                ))}
              </select>
            </label>

            <label className="checkbox-label ordinances-archive-toggle">
              <input
                checked={filterDraft.includeArchived}
                onChange={(event) =>
                  updateFilterDraft({ includeArchived: event.target.checked })
                }
                type="checkbox"
              />
              Incluir archivadas
            </label>

            <div className="button-row">
              <button type="submit" disabled={isLoadingAdmin}>
                Aplicar filtros
              </button>
              <button
                className="secondary-button"
                type="button"
                onClick={handleClearFilters}
                disabled={isLoadingAdmin}
              >
                Limpiar
              </button>
            </div>
          </form>

          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Municipio</th>
                  <th>Título</th>
                  <th>Tema</th>
                  <th>Subtema</th>
                  <th>Tipo</th>
                  <th>Estado</th>
                  <th>Revisión</th>
                  <th>Fuente oficial</th>
                  <th>Boletín oficial</th>
                  <th>Número</th>
                  <th>Fecha de aprobación</th>
                  <th>Fecha de publicación</th>
                  <th>Fecha de vigencia</th>
                  <th>Documento ID</th>
                  <th>Resumen</th>
                  <th>Texto de la ordenanza</th>
                  <th>Notas</th>
                  <th>Notas jurídicas</th>
                  <th>Acción</th>
                </tr>
              </thead>
              <tbody>
                {ordinances.length > 0 ? (
                  ordinances.map((ordinance) => {
                    const edit = ordinanceEdits[ordinance.id] ?? {
                      municipality_id: String(ordinance.municipality_id),
                      document_id:
                        ordinance.document_id === null
                          ? ""
                          : String(ordinance.document_id),
                      title: ordinance.title,
                      topic: ordinance.topic,
                      subtopic: ordinance.subtopic ?? "",
                      ordinance_type: ordinance.ordinance_type,
                      summary: ordinance.summary ?? "",
                      source_url: ordinance.source_url ?? "",
                      official_bulletin: ordinance.official_bulletin ?? "",
                      bulletin_number: ordinance.bulletin_number ?? "",
                      approval_date: ordinance.approval_date ?? "",
                      publication_date: ordinance.publication_date ?? "",
                      effective_date: ordinance.effective_date ?? "",
                      status: ordinance.status,
                      curation_status: ordinance.curation_status,
                      text_content: ordinance.text_content ?? "",
                      notes: ordinance.notes ?? "",
                      legal_review_notes: ordinance.legal_review_notes ?? "",
                    };
                    // The list omits text_content, so a row only becomes
                    // editable after loading the full detail with "Editar".
                    const isRowEditing =
                      (canEdit || canReview) &&
                      Boolean(ordinanceDetailLoaded[ordinance.id]);
                    const canEditRowContent = canEdit && isRowEditing;
                    const isLoadingDetail =
                      loadingOrdinanceDetailId === ordinance.id;

                    return (
                      <tr key={ordinance.id}>
                        <td>{ordinance.id}</td>
                        <td>
                          {canEditRowContent ? (
                            <select
                              aria-label={`Municipio de ${ordinance.title}`}
                              className="table-input"
                              onChange={(event) =>
                                onUpdateOrdinanceEdit(ordinance.id, {
                                  municipality_id: event.target.value,
                                })
                              }
                              value={edit.municipality_id}
                            >
                              {activeMunicipalityOptions.map((municipality) => (
                                <option
                                  key={municipality.id}
                                  value={municipality.id}
                                >
                                  {formatMunicipalityOption(municipality)}
                                </option>
                              ))}
                            </select>
                          ) : (
                            formatMunicipalityOption(ordinance.municipality)
                          )}
                        </td>
                        <EditableTextCell
                          canEdit={canEditRowContent}
                          label={`Título de ${ordinance.title}`}
                          value={edit.title}
                          onChange={(value) =>
                            onUpdateOrdinanceEdit(ordinance.id, {
                              title: value,
                            })
                          }
                        />
                        <EditableTextCell
                          canEdit={canEditRowContent}
                          label={`Tema de ${ordinance.title}`}
                          value={edit.topic}
                          onChange={(value) =>
                            onUpdateOrdinanceEdit(ordinance.id, {
                              topic: value,
                            })
                          }
                        />
                        <EditableTextCell
                          canEdit={canEditRowContent}
                          label={`Subtema de ${ordinance.title}`}
                          value={edit.subtopic}
                          onChange={(value) =>
                            onUpdateOrdinanceEdit(ordinance.id, {
                              subtopic: value,
                            })
                          }
                        />
                        <td>
                          {canEditRowContent ? (
                            <select
                              aria-label={`Tipo de ${ordinance.title}`}
                              className="table-input"
                              onChange={(event) =>
                                onUpdateOrdinanceEdit(ordinance.id, {
                                  ordinance_type: event.target
                                    .value as OrdinanceType,
                                })
                              }
                              value={edit.ordinance_type}
                            >
                              {ORDINANCE_TYPES.map((ordinanceType) => (
                                <option
                                  key={ordinanceType}
                                  value={ordinanceType}
                                >
                                  {formatOrdinanceType(ordinanceType)}
                                </option>
                              ))}
                            </select>
                          ) : (
                            formatOrdinanceType(ordinance.ordinance_type)
                          )}
                        </td>
                        <td>
                          {isRowEditing && canReview ? (
                            <select
                              aria-label={`Estado de ${ordinance.title}`}
                              className="table-input"
                              onChange={(event) =>
                                onUpdateOrdinanceEdit(ordinance.id, {
                                  status: event.target
                                    .value as OrdinanceStatus,
                                })
                              }
                              value={edit.status}
                            >
                              {ORDINANCE_STATUSES.filter(
                                (status) =>
                                  status !== "archived" ||
                                  canArchive ||
                                  edit.status === "archived",
                              ).map((status) => (
                                <option key={status} value={status}>
                                  {formatOrdinanceStatus(status)}
                                </option>
                              ))}
                            </select>
                          ) : (
                            formatOrdinanceStatus(ordinance.status)
                          )}
                        </td>
                        <td>
                          {isRowEditing && canReview ? (
                            <select
                              aria-label={`Revisión de ${ordinance.title}`}
                              className="table-input"
                              onChange={(event) =>
                                onUpdateOrdinanceEdit(ordinance.id, {
                                  curation_status: event.target
                                    .value as OrdinanceCurationStatus,
                                })
                              }
                              value={edit.curation_status}
                            >
                              {ORDINANCE_CURATION_STATUSES.map((status) => (
                                <option key={status} value={status}>
                                  {formatOrdinanceCurationStatus(status)}
                                </option>
                              ))}
                            </select>
                          ) : (
                            formatOrdinanceCurationStatus(
                              ordinance.curation_status,
                            )
                          )}
                        </td>
                        <EditableTextCell
                          canEdit={canEditRowContent}
                          label={`Fuente oficial de ${ordinance.title}`}
                          type="url"
                          value={edit.source_url}
                          onChange={(value) =>
                            onUpdateOrdinanceEdit(ordinance.id, {
                              source_url: value,
                            })
                          }
                        />
                        <EditableTextCell
                          canEdit={canEditRowContent}
                          label={`Boletín oficial de ${ordinance.title}`}
                          value={edit.official_bulletin}
                          onChange={(value) =>
                            onUpdateOrdinanceEdit(ordinance.id, {
                              official_bulletin: value,
                            })
                          }
                        />
                        <EditableTextCell
                          canEdit={canEditRowContent}
                          label={`Número de boletín de ${ordinance.title}`}
                          value={edit.bulletin_number}
                          onChange={(value) =>
                            onUpdateOrdinanceEdit(ordinance.id, {
                              bulletin_number: value,
                            })
                          }
                        />
                        <EditableTextCell
                          canEdit={canEditRowContent}
                          label={`Fecha de aprobación de ${ordinance.title}`}
                          type="date"
                          value={edit.approval_date}
                          onChange={(value) =>
                            onUpdateOrdinanceEdit(ordinance.id, {
                              approval_date: value,
                            })
                          }
                        />
                        <EditableTextCell
                          canEdit={canEditRowContent}
                          label={`Fecha de publicación de ${ordinance.title}`}
                          type="date"
                          value={edit.publication_date}
                          onChange={(value) =>
                            onUpdateOrdinanceEdit(ordinance.id, {
                              publication_date: value,
                            })
                          }
                        />
                        <EditableTextCell
                          canEdit={canEditRowContent}
                          label={`Fecha de vigencia de ${ordinance.title}`}
                          type="date"
                          value={edit.effective_date}
                          onChange={(value) =>
                            onUpdateOrdinanceEdit(ordinance.id, {
                              effective_date: value,
                            })
                          }
                        />
                        <EditableTextCell
                          canEdit={canEditRowContent}
                          inputMode="numeric"
                          label={`Documento ID de ${ordinance.title}`}
                          type="number"
                          value={edit.document_id}
                          onChange={(value) =>
                            onUpdateOrdinanceEdit(ordinance.id, {
                              document_id: value,
                            })
                          }
                        />
                        <EditableTextareaCell
                          canEdit={canEditRowContent}
                          label={`Resumen de ${ordinance.title}`}
                          value={edit.summary}
                          onChange={(value) =>
                            onUpdateOrdinanceEdit(ordinance.id, {
                              summary: value,
                            })
                          }
                        />
                        <td>
                          {canEditRowContent ? (
                            <textarea
                              aria-label={`Texto de la ordenanza de ${ordinance.title}`}
                              className="table-textarea"
                              onChange={(event) =>
                                onUpdateOrdinanceEdit(ordinance.id, {
                                  text_content: event.target.value,
                                })
                              }
                              rows={2}
                              value={edit.text_content}
                            />
                          ) : (
                            <span className="small-muted">
                              {canEdit
                                ? "Pulsa «Editar» para cargar el texto"
                                : "Disponible en el detalle"}
                            </span>
                          )}
                        </td>
                        <EditableTextareaCell
                          canEdit={canEditRowContent}
                          label={`Notas de ${ordinance.title}`}
                          value={edit.notes}
                          onChange={(value) =>
                            onUpdateOrdinanceEdit(ordinance.id, {
                              notes: value,
                            })
                          }
                        />
                        <EditableTextareaCell
                          canEdit={canEditRowContent}
                          label={`Notas jurídicas de ${ordinance.title}`}
                          value={edit.legal_review_notes}
                          onChange={(value) =>
                            onUpdateOrdinanceEdit(ordinance.id, {
                              legal_review_notes: value,
                            })
                          }
                        />
                        <td>
                          <div className="table-actions">
                            {ordinance.curation_status === "approved" ? (
                              <Link
                                href={`/ordenanzas?id=${ordinance.id}${
                                  ["repealed", "superseded", "archived"].includes(
                                    ordinance.status,
                                  )
                                    ? "&include_inactive=true"
                                    : ""
                                }`}
                              >
                                Ver ficha
                              </Link>
                            ) : null}
                            {(canEdit || canReview) && !isRowEditing ? (
                              <button
                                type="button"
                                onClick={() =>
                                  onStartOrdinanceEdit(ordinance.id)
                                }
                                disabled={isLoadingDetail || isLoadingAdmin}
                              >
                                {isLoadingDetail
                                  ? "Cargando..."
                                  : canEdit
                                    ? "Editar"
                                    : "Revisar"}
                              </button>
                            ) : null}
                            {(canEdit || canReview) && isRowEditing ? (
                              <button
                                type="button"
                                onClick={() => onUpdateOrdinance(ordinance.id)}
                                disabled={
                                  updatingOrdinanceId === ordinance.id ||
                                  isLoadingAdmin
                                }
                              >
                                {updatingOrdinanceId === ordinance.id
                                  ? "Guardando..."
                                  : "Guardar"}
                              </button>
                            ) : null}
                            {canArchive ? (
                              <button
                                className="danger-button"
                                type="button"
                                onClick={() => onArchiveOrdinance(ordinance)}
                                disabled={
                                  ordinance.status === "archived" ||
                                  updatingOrdinanceId === ordinance.id ||
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
                    <td colSpan={20}>No hay ordenanzas para mostrar.</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="pager-row">
            <button
              className="secondary-button"
              type="button"
              onClick={onOrdinancePrevPage}
              disabled={ordinancePage === 0 || isLoadingAdmin}
            >
              Anterior
            </button>
            <span className="pager-status">
              Página {ordinancePage + 1} de {ordinancePageCount} (
              {ordinanceTotal} en total)
            </span>
            <button
              className="secondary-button"
              type="button"
              onClick={onOrdinanceNextPage}
              disabled={
                ordinancePage + 1 >= ordinancePageCount || isLoadingAdmin
              }
            >
              Siguiente
            </button>
          </div>
        </>
      ) : (
        <p className="small-muted">No tienes permisos para listar ordenanzas.</p>
      )}

      {ordinanceEditError ? (
        <p className="error-message">{ordinanceEditError}</p>
      ) : null}
      {ordinanceEditMessage ? (
        <p className="success-message">{ordinanceEditMessage}</p>
      ) : null}

      {canCreate ? (
        <form className="admin-form" onSubmit={onCreateOrdinance}>
          <h4>Nueva ordenanza</h4>
          <div className="form-grid">
            <OrdinanceFormFields
              canArchive={canArchive}
              canReview={canReview}
              edit={newOrdinance}
              municipalities={activeMunicipalityOptions}
              onUpdate={onUpdateNewOrdinance}
            />
          </div>

          {ordinanceFormError ? (
            <p className="error-message">{ordinanceFormError}</p>
          ) : null}

          <button
            type="submit"
            disabled={
              isCreatingOrdinance || activeMunicipalityOptions.length === 0
            }
          >
            {isCreatingOrdinance ? "Creando..." : "Crear ordenanza"}
          </button>
        </form>
      ) : null}
    </div>
  );
}

function getMunicipalityOptions(
  municipalities: Municipality[],
  ordinances: Ordinance[],
) {
  if (municipalities.length > 0) {
    return municipalities;
  }

  const options = new Map<number, Ordinance["municipality"]>();
  ordinances.forEach((ordinance) => {
    options.set(ordinance.municipality.id, ordinance.municipality);
  });

  return Array.from(options.values()).sort((first, second) =>
    formatMunicipalityOption(first).localeCompare(
      formatMunicipalityOption(second),
      "es",
    ),
  );
}

function EditableTextCell({
  canEdit,
  inputMode,
  label,
  type = "text",
  value,
  onChange,
}: {
  canEdit: boolean;
  inputMode?: "numeric";
  label: string;
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
          step={type === "number" ? "1" : undefined}
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

function OrdinanceFormFields({
  canArchive,
  canReview,
  edit,
  municipalities,
  onUpdate,
}: {
  canArchive: boolean;
  canReview: boolean;
  edit: OrdinanceEditState;
  municipalities: ReturnType<typeof getMunicipalityOptions>;
  onUpdate: (updates: Partial<OrdinanceEditState>) => void;
}) {
  return (
    <>
      <label>
        Municipio
        <select
          onChange={(event) => onUpdate({ municipality_id: event.target.value })}
          required
          value={edit.municipality_id}
        >
          <option value="">Selecciona un municipio</option>
          {municipalities.map((municipality) => (
            <option key={municipality.id} value={municipality.id}>
              {formatMunicipalityOption(municipality)}
            </option>
          ))}
        </select>
      </label>

      <label>
        Título
        <input
          onChange={(event) => onUpdate({ title: event.target.value })}
          required
          type="text"
          value={edit.title}
        />
      </label>

      <label>
        Tema
        <input
          onChange={(event) => onUpdate({ topic: event.target.value })}
          required
          type="text"
          value={edit.topic}
        />
      </label>

      <label>
        Subtema
        <input
          onChange={(event) => onUpdate({ subtopic: event.target.value })}
          type="text"
          value={edit.subtopic}
        />
      </label>

      <label>
        Tipo
        <select
          onChange={(event) =>
            onUpdate({ ordinance_type: event.target.value as OrdinanceType })
          }
          required
          value={edit.ordinance_type}
        >
          {ORDINANCE_TYPES.map((ordinanceType) => (
            <option key={ordinanceType} value={ordinanceType}>
              {formatOrdinanceType(ordinanceType)}
            </option>
          ))}
        </select>
      </label>

      {canReview ? (
        <>
          <label>
            Estado
            <select
              onChange={(event) =>
                onUpdate({ status: event.target.value as OrdinanceStatus })
              }
              required
              value={edit.status}
            >
              {ORDINANCE_STATUSES.filter(
                (status) => status !== "archived" || canArchive,
              ).map((status) => (
                <option key={status} value={status}>
                  {formatOrdinanceStatus(status)}
                </option>
              ))}
            </select>
          </label>

          <label>
            Revisión
            <select
              onChange={(event) =>
                onUpdate({
                  curation_status: event.target.value as OrdinanceCurationStatus,
                })
              }
              required
              value={edit.curation_status}
            >
              {ORDINANCE_CURATION_STATUSES.map((status) => (
                <option key={status} value={status}>
                  {formatOrdinanceCurationStatus(status)}
                </option>
              ))}
            </select>
          </label>
        </>
      ) : null}

      <label>
        Fuente oficial
        <input
          onChange={(event) => onUpdate({ source_url: event.target.value })}
          type="url"
          value={edit.source_url}
        />
      </label>

      <label>
        Boletín oficial
        <input
          onChange={(event) =>
            onUpdate({ official_bulletin: event.target.value })
          }
          type="text"
          value={edit.official_bulletin}
        />
      </label>

      <label>
        Número de boletín
        <input
          onChange={(event) => onUpdate({ bulletin_number: event.target.value })}
          type="text"
          value={edit.bulletin_number}
        />
      </label>

      <label>
        Fecha de aprobación
        <input
          onChange={(event) => onUpdate({ approval_date: event.target.value })}
          type="date"
          value={edit.approval_date}
        />
      </label>

      <label>
        Fecha de publicación
        <input
          onChange={(event) =>
            onUpdate({ publication_date: event.target.value })
          }
          type="date"
          value={edit.publication_date}
        />
      </label>

      <label>
        Fecha de vigencia
        <input
          onChange={(event) => onUpdate({ effective_date: event.target.value })}
          type="date"
          value={edit.effective_date}
        />
      </label>

      <label>
        Documento ID
        <input
          min="0"
          onChange={(event) => onUpdate({ document_id: event.target.value })}
          step="1"
          type="number"
          value={edit.document_id}
        />
      </label>

      <label>
        Resumen
        <textarea
          onChange={(event) => onUpdate({ summary: event.target.value })}
          rows={3}
          value={edit.summary}
        />
      </label>

      <label>
        Texto de la ordenanza
        <textarea
          onChange={(event) => onUpdate({ text_content: event.target.value })}
          rows={5}
          value={edit.text_content}
        />
      </label>

      <label>
        Notas
        <textarea
          onChange={(event) => onUpdate({ notes: event.target.value })}
          rows={3}
          value={edit.notes}
        />
      </label>

      <label>
        Notas jurídicas
        <textarea
          onChange={(event) =>
            onUpdate({ legal_review_notes: event.target.value })
          }
          rows={3}
          value={edit.legal_review_notes}
        />
      </label>
    </>
  );
}
