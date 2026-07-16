import { Check, ExternalLink, MapPin, Plus, ShieldAlert } from "lucide-react";
import {
  formatLegalDate,
  formatPopulation,
  ordinanceStatusNote,
  safeExternalUrl,
} from "../lib/ordinance-format";
import type {
  OrdinanceSearchPage,
  OrdinanceSemanticSearchResult,
} from "./types";
import { OrdinanceStatusBadge } from "./OrdinanceStatusBadge";
import styles from "./OrdinanceLibrary.module.css";

type OrdinanceSearchResultsProps = {
  page: OrdinanceSearchPage;
  selectedOrdinanceId: number | null;
  comparisonMunicipalityIds: number[];
  canCompare: boolean;
  onSelect: (ordinanceId: number, trigger: HTMLElement) => void;
  onToggleComparison: (result: OrdinanceSemanticSearchResult) => void;
};

export function OrdinanceSearchResults({
  page,
  selectedOrdinanceId,
  comparisonMunicipalityIds,
  canCompare,
  onSelect,
  onToggleComparison,
}: OrdinanceSearchResultsProps) {
  if (page.results.length === 0) {
    return (
      <div className={styles.emptyState}>
        <h2>No hay coincidencias revisadas</h2>
        <p>
          Prueba una consulta más amplia, elimina algún filtro o cambia el
          ámbito de resultados. Las normas pendientes de revisión no aparecen
          en esta biblioteca.
        </p>
      </div>
    );
  }

  return (
    <div className={styles.resultList}>
      {page.results.map((result, index) => {
        const comparisonSelected = comparisonMunicipalityIds.includes(
          result.municipality_id,
        );
        const sourceUrl = safeExternalUrl(result.source_url);
        const referenceDate = result.effective_date ?? result.publication_date;
        const statusNote = ordinanceStatusNote(result.status);

        return (
          <article
            className={`${styles.resultCard}${
              selectedOrdinanceId === result.ordinance_id
                ? ` ${styles.resultCardSelected}`
                : ""
            }`}
            key={`${result.ordinance_id}-${result.chunk_id}`}
          >
            <div className={styles.resultTopline}>
              <span className={styles.resultOrder}>
                {page.offset + index + 1}
              </span>
              <span className={styles.topicLabel}>{result.topic}</span>
              <OrdinanceStatusBadge status={result.status} />
            </div>

            <button
              className={styles.resultTitleButton}
              onClick={(event) =>
                onSelect(result.ordinance_id, event.currentTarget)
              }
              type="button"
            >
              <span>{result.title}</span>
              <small>Ver ficha jurídica y texto revisado</small>
            </button>

            <p className={styles.municipalityLine}>
              <MapPin aria-hidden="true" />
              <strong>{result.municipality_name}</strong>
              <span>{result.province}</span>
              <span>{formatPopulation(result.population)}</span>
            </p>

            {statusNote ? (
              <p className={styles.legalWarning} role="status">
                <ShieldAlert aria-hidden="true" />
                {statusNote}
              </p>
            ) : null}

            <div className={styles.matchExcerpt}>
              <span>
                {result.citation ??
                  result.heading ??
                  result.source_locator ??
                  `Fragmento ${result.chunk_index + 1}`}
              </span>
              <p>{result.text}</p>
              {result.text_truncated ? (
                <small>Extracto abreviado; el texto revisado está en la ficha.</small>
              ) : null}
            </div>

            <footer className={styles.resultFooter}>
              <span>
                {referenceDate
                  ? `Referencia: ${formatLegalDate(referenceDate)}`
                  : "Sin fecha de vigencia o publicación"}
              </span>
              <div className={styles.resultActions}>
                {sourceUrl ? (
                  <a href={sourceUrl} rel="noreferrer" target="_blank">
                    Abrir fuente
                    <ExternalLink aria-hidden="true" />
                  </a>
                ) : null}
                {canCompare ? (
                  <button
                    aria-pressed={comparisonSelected}
                    className={styles.compareToggle}
                    onClick={() => onToggleComparison(result)}
                    type="button"
                  >
                    {comparisonSelected ? (
                      <Check aria-hidden="true" />
                    ) : (
                      <Plus aria-hidden="true" />
                    )}
                    {comparisonSelected ? "En comparación" : "Comparar municipio"}
                  </button>
                ) : null}
              </div>
            </footer>
          </article>
        );
      })}
    </div>
  );
}
