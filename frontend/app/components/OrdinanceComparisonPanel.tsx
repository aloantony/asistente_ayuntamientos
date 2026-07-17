import { ExternalLink, Scale, X } from "lucide-react";
import {
  formatLegalDate,
  safeExternalUrl,
} from "../lib/ordinance-format";
import type { OrdinanceComparison } from "./types";
import { OrdinanceStatusBadge } from "./OrdinanceStatusBadge";
import styles from "./OrdinanceLibrary.module.css";

type OrdinanceComparisonPanelProps = {
  comparison: OrdinanceComparison | null;
  isLoading: boolean;
  error: string;
  onClose: () => void;
  onSelectOrdinance: (ordinanceId: number, trigger: HTMLElement) => void;
};

export function OrdinanceComparisonPanel({
  comparison,
  isLoading,
  error,
  onClose,
  onSelectOrdinance,
}: OrdinanceComparisonPanelProps) {
  return (
    <section
      aria-busy={isLoading}
      aria-labelledby="ordinance-comparison-heading"
      className={styles.comparisonPanel}
    >
      <header className={styles.comparisonHeader}>
        <div>
          <p className={styles.sectionEyebrow}>Lectura transversal</p>
          <h2 id="ordinance-comparison-heading">
            <Scale aria-hidden="true" />
            Comparación municipal
          </h2>
          <p>
            Contrasta materias, fechas y estados. La ausencia de una fila no
            demuestra que el municipio carezca de regulación.
          </p>
        </div>
        <button aria-label="Cerrar comparación" onClick={onClose} type="button">
          <X aria-hidden="true" />
        </button>
      </header>

      {isLoading ? (
        <p className={styles.comparisonState} role="status">
          Preparando la comparación del corpus revisado…
        </p>
      ) : null}

      {!isLoading && error ? (
        <p className={styles.inlineError} role="alert">
          {error}
        </p>
      ) : null}

      {!isLoading && !error && comparison ? (
        comparison.rows.length > 0 ? (
          <div className={styles.comparisonTableWrapper}>
            <table className={styles.comparisonTable}>
              <thead>
                <tr>
                  <th scope="col">Materia</th>
                  {comparison.municipalities.map((municipality) => (
                    <th key={municipality.id} scope="col">
                      <strong>{municipality.name}</strong>
                      <span>{municipality.province}</span>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {comparison.rows.map((row) => (
                  <tr key={`${row.topic}-${row.subtopic ?? ""}`}>
                    <th scope="row">
                      <strong>{row.topic}</strong>
                      {row.subtopic ? <span>{row.subtopic}</span> : null}
                    </th>
                    {comparison.municipalities.map((municipality) => {
                      const entries = row.entries.filter(
                        (entry) => entry.municipality_id === municipality.id,
                      );
                      return (
                        <td key={municipality.id}>
                          {entries.length > 0 ? (
                            <div className={styles.comparisonEntries}>
                              {entries.map((entry) => {
                                const sourceUrl = safeExternalUrl(entry.source_url);
                                return (
                                  <article key={entry.ordinance_id}>
                                    <OrdinanceStatusBadge status={entry.status} />
                                    <button
                                      onClick={(event) =>
                                        onSelectOrdinance(
                                          entry.ordinance_id,
                                          event.currentTarget,
                                        )
                                      }
                                      type="button"
                                    >
                                      {entry.title}
                                    </button>
                                    <dl>
                                      <div>
                                        <dt>Aprobación</dt>
                                        <dd>
                                          {formatLegalDate(entry.approval_date)}
                                        </dd>
                                      </div>
                                      <div>
                                        <dt>Vigencia</dt>
                                        <dd>
                                          {formatLegalDate(entry.effective_date)}
                                        </dd>
                                      </div>
                                    </dl>
                                    {entry.summary ? <p>{entry.summary}</p> : null}
                                    {sourceUrl ? (
                                      <a
                                        href={sourceUrl}
                                        rel="noreferrer"
                                        target="_blank"
                                      >
                                        Fuente
                                        <ExternalLink aria-hidden="true" />
                                      </a>
                                    ) : null}
                                  </article>
                                );
                              })}
                            </div>
                          ) : (
                            <span className={styles.noComparisonEntry}>
                              Sin registro aprobado en este corpus
                            </span>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className={styles.comparisonState}>
            No hay ordenanzas aprobadas que coincidan con esta selección y sus
            filtros actuales.
          </p>
        )
      ) : null}
    </section>
  );
}
