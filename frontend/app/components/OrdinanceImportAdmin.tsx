"use client";

import { useEffect, useMemo, useState, type FormEvent } from "react";
import { adminRequest } from "../lib/api";
import { useSession } from "../lib/session";
import {
  formatMunicipalityOption,
  formatOrdinanceCurationStatus,
  formatOrdinanceImportItemStatus,
  formatOrdinanceImportJobStatus,
  userHasPermission,
  type Municipality,
  type OfficialLegalSource,
  type OrdinanceComparison,
  type OrdinanceImportJob,
  type OrdinanceReviewDecision,
} from "./types";

type OrdinanceImportAdminProps = {
  municipalities: Municipality[];
};

const DEFAULT_REVIEW_CRITERIA =
  "Comprobar que la fuente es oficial, que el municipio coincide, que el texto corresponde a una ordenanza municipal y que los metadatos principales son trazables.";

export function OrdinanceImportAdmin({
  municipalities,
}: OrdinanceImportAdminProps) {
  const { user, getStoredToken, handleRequestError } = useSession();
  const [sources, setSources] = useState<OfficialLegalSource[]>([]);
  const [jobs, setJobs] = useState<OrdinanceImportJob[]>([]);
  const [selectedJob, setSelectedJob] = useState<OrdinanceImportJob | null>(
    null,
  );
  const [importError, setImportError] = useState("");
  const [importMessage, setImportMessage] = useState("");
  const [isLoadingImports, setIsLoadingImports] = useState(false);
  const [jobTitle, setJobTitle] = useState("");
  const [jobTopic, setJobTopic] = useState("");
  const [jobSearchQuery, setJobSearchQuery] = useState("");
  const [jobSourceUrls, setJobSourceUrls] = useState("");
  const [jobReviewCriteria, setJobReviewCriteria] = useState(
    DEFAULT_REVIEW_CRITERIA,
  );
  const [selectedMunicipalityIds, setSelectedMunicipalityIds] = useState<
    number[]
  >([]);
  const [selectedSourceIds, setSelectedSourceIds] = useState<number[]>([]);
  const [comparisonTopic, setComparisonTopic] = useState("");
  const [comparisonMunicipalityIds, setComparisonMunicipalityIds] = useState<
    number[]
  >([]);
  const [comparisonIncludePending, setComparisonIncludePending] =
    useState(false);
  const [comparison, setComparison] = useState<OrdinanceComparison | null>(
    null,
  );
  const [comparisonError, setComparisonError] = useState("");

  const canImport = Boolean(
    user &&
      (userHasPermission(user, "ordinances.import") ||
        userHasPermission(user, "ordinances.manage")),
  );
  const canReview = Boolean(
    user &&
      (userHasPermission(user, "ordinances.review") ||
        userHasPermission(user, "ordinances.manage")),
  );
  const canCompare = Boolean(
    user &&
      (userHasPermission(user, "ordinances.compare") ||
        userHasPermission(user, "ordinances.manage")),
  );

  const activeMunicipalities = useMemo(
    () =>
      municipalities
        .filter((municipality) => municipality.status === "active")
        .sort((first, second) =>
          formatMunicipalityOption(first).localeCompare(
            formatMunicipalityOption(second),
            "es",
          ),
        ),
    [municipalities],
  );

  useEffect(() => {
    if (!canImport) {
      return;
    }
    void loadImportData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canImport]);

  async function loadImportData() {
    setIsLoadingImports(true);
    setImportError("");
    try {
      const token = getStoredToken();
      const [sourceData, jobData] = await Promise.all([
        adminRequest<OfficialLegalSource[]>(
          "/ordinances/official-sources",
          token,
          "No se pudieron cargar las fuentes oficiales.",
        ),
        adminRequest<OrdinanceImportJob[]>(
          "/ordinances/import-jobs",
          token,
          "No se pudieron cargar las importaciones.",
        ),
      ]);
      setSources(sourceData);
      setJobs(jobData);
    } catch (error) {
      handleRequestError(
        error,
        setImportError,
        "No se pudieron cargar las importaciones.",
      );
    } finally {
      setIsLoadingImports(false);
    }
  }

  async function loadJobDetail(jobId: number) {
    setImportError("");
    try {
      const detail = await adminRequest<OrdinanceImportJob>(
        `/ordinances/import-jobs/${jobId}`,
        getStoredToken(),
        "No se pudo cargar la importación.",
      );
      setSelectedJob(detail);
    } catch (error) {
      handleRequestError(
        error,
        setImportError,
        "No se pudo cargar la importación.",
      );
    }
  }

  async function handleCreateJob(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setImportError("");
    setImportMessage("");
    const sourceUrls = jobSourceUrls
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => ({
        url: line,
        municipality_id:
          selectedMunicipalityIds.length === 1
            ? selectedMunicipalityIds[0]
            : null,
        official_source_id:
          selectedSourceIds.length === 1 ? selectedSourceIds[0] : null,
        title: null,
      }));

    try {
      const created = await adminRequest<OrdinanceImportJob>(
        "/ordinances/import-jobs",
        getStoredToken(),
        "No se pudo crear la importación.",
        {
          method: "POST",
          body: JSON.stringify({
            title: jobTitle,
            topic: jobTopic || null,
            search_query: jobSearchQuery || null,
            municipality_ids: selectedMunicipalityIds,
            official_source_ids: selectedSourceIds,
            source_urls: sourceUrls,
            review_criteria: jobReviewCriteria,
          }),
        },
      );
      setJobTitle("");
      setJobTopic("");
      setJobSearchQuery("");
      setJobSourceUrls("");
      setSelectedJob(created);
      setImportMessage("Importación creada.");
      await loadImportData();
    } catch (error) {
      handleRequestError(
        error,
        setImportError,
        "No se pudo crear la importación.",
      );
    }
  }

  async function handleEnqueueJob(jobId: number) {
    setImportError("");
    setImportMessage("");
    try {
      await adminRequest(
        `/ordinances/import-jobs/${jobId}/enqueue`,
        getStoredToken(),
        "No se pudo encolar la importación.",
        { method: "POST" },
      );
      setImportMessage("Importación encolada.");
      await loadImportData();
      await loadJobDetail(jobId);
    } catch (error) {
      handleRequestError(
        error,
        setImportError,
        "No se pudo encolar la importación.",
      );
    }
  }

  async function handleRunInline(jobId: number) {
    setImportError("");
    setImportMessage("");
    try {
      const detail = await adminRequest<OrdinanceImportJob>(
        `/ordinances/import-jobs/${jobId}/run-inline`,
        getStoredToken(),
        "No se pudo ejecutar la importación.",
        { method: "POST" },
      );
      setSelectedJob(detail);
      setImportMessage("Importación ejecutada.");
      await loadImportData();
    } catch (error) {
      handleRequestError(
        error,
        setImportError,
        "No se pudo ejecutar la importación.",
      );
    }
  }

  async function handleReviewItem(
    itemId: number,
    decision: OrdinanceReviewDecision,
  ) {
    setImportError("");
    setImportMessage("");
    try {
      await adminRequest(
        `/ordinances/import-items/${itemId}/review`,
        getStoredToken(),
        "No se pudo revisar la ordenanza importada.",
        {
          method: "PATCH",
          body: JSON.stringify({ decision }),
        },
      );
      setImportMessage("Revisión guardada.");
      if (selectedJob) {
        await loadJobDetail(selectedJob.id);
      }
    } catch (error) {
      handleRequestError(
        error,
        setImportError,
        "No se pudo revisar la ordenanza importada.",
      );
    }
  }

  async function handleLoadComparison(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setComparisonError("");
    const params = new URLSearchParams();
    comparisonMunicipalityIds.forEach((municipalityId) => {
      params.append("municipality_ids", String(municipalityId));
    });
    if (comparisonTopic) {
      params.set("topic", comparisonTopic);
    }
    if (comparisonIncludePending) {
      params.set("include_pending", "true");
    }

    try {
      const data = await adminRequest<OrdinanceComparison>(
        `/ordinances/comparison?${params.toString()}`,
        getStoredToken(),
        "No se pudo comparar las ordenanzas.",
      );
      setComparison(data);
    } catch (error) {
      handleRequestError(
        error,
        setComparisonError,
        "No se pudo comparar las ordenanzas.",
      );
    }
  }

  if (!canImport && !canCompare) {
    return null;
  }

  return (
    <div className="admin-section ordinance-workflows">
      {canImport ? (
        <>
          <div className="section-header">
            <h3>Importaciones</h3>
            <button
              className="secondary-button"
              type="button"
              onClick={() => void loadImportData()}
              disabled={isLoadingImports}
            >
              {isLoadingImports ? "Cargando..." : "Actualizar"}
            </button>
          </div>

          {importError ? <p className="error-message">{importError}</p> : null}
          {importMessage ? (
            <p className="success-message">{importMessage}</p>
          ) : null}

          <form className="admin-form" onSubmit={handleCreateJob}>
            <h4>Nueva importación</h4>
            <div className="form-grid">
              <label>
                Título
                <input
                  onChange={(event) => setJobTitle(event.target.value)}
                  required
                  type="text"
                  value={jobTitle}
                />
              </label>
              <label>
                Tema
                <input
                  onChange={(event) => setJobTopic(event.target.value)}
                  type="text"
                  value={jobTopic}
                />
              </label>
              <label>
                Búsqueda oficial
                <input
                  onChange={(event) => setJobSearchQuery(event.target.value)}
                  type="text"
                  value={jobSearchQuery}
                />
              </label>
              <label>
                Municipios
                <select
                  multiple
                  onChange={(event) =>
                    setSelectedMunicipalityIds(
                      Array.from(event.target.selectedOptions).map((option) =>
                        Number(option.value),
                      ),
                    )
                  }
                  value={selectedMunicipalityIds.map(String)}
                >
                  {activeMunicipalities.map((municipality) => (
                    <option key={municipality.id} value={municipality.id}>
                      {formatMunicipalityOption(municipality)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Fuentes
                <select
                  multiple
                  onChange={(event) =>
                    setSelectedSourceIds(
                      Array.from(event.target.selectedOptions).map((option) =>
                        Number(option.value),
                      ),
                    )
                  }
                  value={selectedSourceIds.map(String)}
                >
                  {sources.map((source) => (
                    <option key={source.id} value={source.id}>
                      {source.name} ({source.domain})
                    </option>
                  ))}
                </select>
              </label>
              <label>
                URLs semilla
                <textarea
                  onChange={(event) => setJobSourceUrls(event.target.value)}
                  rows={4}
                  value={jobSourceUrls}
                />
              </label>
              <label>
                Criterios
                <textarea
                  onChange={(event) => setJobReviewCriteria(event.target.value)}
                  required
                  rows={4}
                  value={jobReviewCriteria}
                />
              </label>
            </div>
            <button type="submit">Crear importación</button>
          </form>

          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Título</th>
                  <th>Estado</th>
                  <th>Items</th>
                  <th>Error</th>
                  <th>Acción</th>
                </tr>
              </thead>
              <tbody>
                {jobs.length > 0 ? (
                  jobs.map((job) => (
                    <tr key={job.id}>
                      <td>{job.id}</td>
                      <td>{job.title}</td>
                      <td>{formatOrdinanceImportJobStatus(job.status)}</td>
                      <td>{job.item_count}</td>
                      <td>{job.error_message ?? ""}</td>
                      <td>
                        <div className="table-actions">
                          <button
                            type="button"
                            onClick={() => void loadJobDetail(job.id)}
                          >
                            Ver
                          </button>
                          <button
                            type="button"
                            onClick={() => void handleEnqueueJob(job.id)}
                            disabled={
                              job.status === "queued" || job.status === "running"
                            }
                          >
                            Encolar
                          </button>
                          <button
                            className="secondary-button"
                            type="button"
                            onClick={() => void handleRunInline(job.id)}
                            disabled={
                              job.status === "queued" || job.status === "running"
                            }
                          >
                            Ejecutar ahora
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={6}>No hay importaciones.</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          {selectedJob ? (
            <div className="admin-section">
              <div className="section-header">
                <h4>Importación #{selectedJob.id}</h4>
                <span className="small-muted">
                  {formatOrdinanceImportJobStatus(selectedJob.status)}
                </span>
              </div>
              <div className="table-wrapper">
                <table>
                  <thead>
                    <tr>
                      <th>ID</th>
                      <th>Fuente</th>
                      <th>Estado</th>
                      <th>Ordenanza</th>
                      <th>Confianza</th>
                      <th>Checklist</th>
                      <th>Acción</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(selectedJob.items ?? []).map((item) => {
                      const report = item.review_reports[0];
                      return (
                        <tr key={item.id}>
                          <td>{item.id}</td>
                          <td>
                            <a href={item.source_url}>{item.source_title ?? item.source_url}</a>
                          </td>
                          <td>
                            {formatOrdinanceImportItemStatus(item.status)}
                          </td>
                          <td>
                            {item.ordinance
                              ? `${item.ordinance.title} (${formatOrdinanceCurationStatus(
                                  item.ordinance.curation_status,
                                )})`
                              : ""}
                          </td>
                          <td>
                            {item.confidence_score === null
                              ? ""
                              : item.confidence_score.toFixed(2)}
                          </td>
                          <td>
                            {report
                              ? report.checklist
                                  .map((entry) =>
                                    entry.passed ? entry.label : `${entry.label}: no`,
                                  )
                                  .join(" · ")
                              : item.error_message ?? ""}
                          </td>
                          <td>
                            {canReview && item.ordinance ? (
                              <div className="table-actions">
                                <button
                                  type="button"
                                  onClick={() =>
                                    void handleReviewItem(item.id, "approve")
                                  }
                                >
                                  Aprobar
                                </button>
                                <button
                                  className="secondary-button"
                                  type="button"
                                  onClick={() =>
                                    void handleReviewItem(item.id, "needs_changes")
                                  }
                                >
                                  Cambios
                                </button>
                                <button
                                  className="danger-button"
                                  type="button"
                                  onClick={() =>
                                    void handleReviewItem(item.id, "reject")
                                  }
                                >
                                  Rechazar
                                </button>
                              </div>
                            ) : null}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          ) : null}
        </>
      ) : null}

      {canCompare ? (
        <div className="admin-section">
          <div className="section-header">
            <h3>Comparación</h3>
          </div>
          <form className="admin-form" onSubmit={handleLoadComparison}>
            <div className="form-grid">
              <label>
                Municipios
                <select
                  multiple
                  onChange={(event) =>
                    setComparisonMunicipalityIds(
                      Array.from(event.target.selectedOptions).map((option) =>
                        Number(option.value),
                      ),
                    )
                  }
                  required
                  value={comparisonMunicipalityIds.map(String)}
                >
                  {activeMunicipalities.map((municipality) => (
                    <option key={municipality.id} value={municipality.id}>
                      {formatMunicipalityOption(municipality)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Tema
                <input
                  onChange={(event) => setComparisonTopic(event.target.value)}
                  type="text"
                  value={comparisonTopic}
                />
              </label>
              {canReview ? (
                <label className="checkbox-label">
                  <input
                    checked={comparisonIncludePending}
                    onChange={(event) =>
                      setComparisonIncludePending(event.target.checked)
                    }
                    type="checkbox"
                  />
                  Incluir pendientes
                </label>
              ) : null}
            </div>
            {comparisonError ? (
              <p className="error-message">{comparisonError}</p>
            ) : null}
            <button type="submit">Comparar</button>
          </form>

          {comparison ? (
            <div className="table-wrapper">
              <table>
                <thead>
                  <tr>
                    <th>Tema</th>
                    <th>Municipio</th>
                    <th>Ordenanza</th>
                    <th>Estado</th>
                    <th>Publicación</th>
                    <th>Fuente</th>
                  </tr>
                </thead>
                <tbody>
                  {comparison.rows.flatMap((row) =>
                    row.entries.map((entry) => (
                      <tr key={`${row.topic}-${entry.ordinance_id}`}>
                        <td>
                          {row.topic}
                          {row.subtopic ? ` / ${row.subtopic}` : ""}
                        </td>
                        <td>{entry.municipality_name}</td>
                        <td>{entry.title}</td>
                        <td>
                          {formatOrdinanceCurationStatus(entry.curation_status)}
                        </td>
                        <td>{entry.publication_date ?? ""}</td>
                        <td>
                          {entry.source_url ? (
                            <a href={entry.source_url}>Fuente</a>
                          ) : null}
                        </td>
                      </tr>
                    )),
                  )}
                </tbody>
              </table>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
