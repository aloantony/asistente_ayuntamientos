"use client";

import { ExternalLink, FileText, ShieldAlert, X } from "lucide-react";
import { useEffect, useRef } from "react";
import {
  formatLegalDate,
  ordinanceStatusNote,
  safeExternalUrl,
} from "../lib/ordinance-format";
import type { Ordinance, OrdinanceLegalChunk } from "./types";
import { OrdinanceStatusBadge } from "./OrdinanceStatusBadge";
import styles from "./OrdinanceLibrary.module.css";

type OrdinanceDetailPanelProps = {
  detail: { ordinance: Ordinance; chunks: OrdinanceLegalChunk[] } | null;
  focusKey: number;
  isLoading: boolean;
  error: string;
  onClose: () => void;
};

export function OrdinanceDetailPanel({
  detail,
  focusKey,
  isLoading,
  error,
  onClose,
}: OrdinanceDetailPanelProps) {
  const panelRef = useRef<HTMLElement>(null);

  useEffect(() => {
    panelRef.current?.focus();
  }, [detail?.ordinance.id, focusKey]);

  return (
    <aside
      aria-label={
        detail
          ? `Ficha jurídica: ${detail.ordinance.title}`
          : "Ficha jurídica de la ordenanza"
      }
      aria-busy={isLoading}
      className={styles.detailPanel}
      ref={panelRef}
      role="region"
      tabIndex={-1}
    >
      <div className={styles.panelToolbar}>
        <span>Ficha jurídica</span>
        <button aria-label="Cerrar ficha" onClick={onClose} type="button">
          <X aria-hidden="true" />
        </button>
      </div>

      {isLoading ? (
        <div className={styles.detailLoading} role="status">
          <span aria-hidden="true" />
          <span aria-hidden="true" />
          <span aria-hidden="true" />
          <p>Cargando ficha y fragmentos revisados…</p>
        </div>
      ) : null}

      {!isLoading && error ? (
        <div className={styles.inlineError} role="alert">
          <h2 id="ordinance-detail-heading">No se pudo abrir la ficha</h2>
          <p>{error}</p>
        </div>
      ) : null}

      {!isLoading && !error && detail ? (
        renderOrdinanceDetailContent(detail)
      ) : null}
    </aside>
  );
}

function renderOrdinanceDetailContent(detail: {
  ordinance: Ordinance;
  chunks: OrdinanceLegalChunk[];
}) {
  const { ordinance, chunks } = detail;
  const sourceUrl = safeExternalUrl(ordinance.source_url);
  const statusNote = ordinanceStatusNote(ordinance.status);

  return (
    <div className={styles.detailContent}>
      <header className={styles.detailHeader}>
        <p className={styles.detailMunicipality}>
          {ordinance.municipality.name} · {ordinance.municipality.province}
        </p>
        <h2 id="ordinance-detail-heading">{ordinance.title}</h2>
        <div className={styles.detailTags}>
          <OrdinanceStatusBadge status={ordinance.status} />
          <span>{ordinance.topic}</span>
          {ordinance.subtopic ? <span>{ordinance.subtopic}</span> : null}
        </div>
      </header>

      {statusNote ? (
        <p className={styles.legalWarning} role="status">
          <ShieldAlert aria-hidden="true" />
          {statusNote}
        </p>
      ) : null}

      <dl className={styles.legalMetadata}>
        <div>
          <dt>Aprobación</dt>
          <dd>{formatLegalDate(ordinance.approval_date)}</dd>
        </div>
        <div>
          <dt>Publicación</dt>
          <dd>{formatLegalDate(ordinance.publication_date)}</dd>
        </div>
        <div>
          <dt>Entrada en vigor</dt>
          <dd>{formatLegalDate(ordinance.effective_date)}</dd>
        </div>
        <div>
          <dt>Boletín</dt>
          <dd>
            {[ordinance.official_bulletin, ordinance.bulletin_number]
              .filter(Boolean)
              .join(" · ") || "No consta"}
          </dd>
        </div>
      </dl>

      {ordinance.summary ? (
        <section className={styles.detailSection}>
          <h3>Resumen revisado</h3>
          <p>{ordinance.summary}</p>
        </section>
      ) : null}

      {sourceUrl ? (
        <a
          className={styles.officialSourceLink}
          href={sourceUrl}
          rel="noreferrer"
          target="_blank"
        >
          <span>
            <strong>Abrir fuente enlazada</strong>
            <small>
              Contrasta su carácter oficial, vigencia, modificaciones y texto íntegro.
            </small>
          </span>
          <ExternalLink aria-hidden="true" />
        </a>
      ) : (
        <p className={styles.sourceMissing}>
          No consta un enlace de referencia seguro en esta ficha.
        </p>
      )}

      {ordinance.legal_review_notes ? (
        <section className={styles.detailSection}>
          <h3>Observaciones jurídicas</h3>
          <p>{ordinance.legal_review_notes}</p>
        </section>
      ) : null}

      <section className={styles.detailSection}>
        <div className={styles.sectionHeadingRow}>
          <div>
            <p className={styles.sectionEyebrow}>Corpus validado</p>
            <h3>Fragmentos revisados</h3>
          </div>
          <span>{chunks.length}</span>
        </div>
        {chunks.length > 0 ? (
          <div className={styles.chunkList}>
            {chunks.map((chunk) => (
              <details key={chunk.id}>
                <summary>
                  <FileText aria-hidden="true" />
                  <span>
                    <strong>
                      {chunk.citation ??
                        chunk.heading ??
                        `Fragmento ${chunk.chunk_index + 1}`}
                    </strong>
                    {chunk.source_locator ? (
                      <small>{chunk.source_locator}</small>
                    ) : null}
                  </span>
                </summary>
                <p>{chunk.text}</p>
              </details>
            ))}
          </div>
        ) : (
          <p className={styles.emptyCopy}>
            Esta ficha aún no tiene fragmentos jurídicos aprobados para
            consulta. No debe usarse como base documental del asistente.
          </p>
        )}
      </section>

      {ordinance.text_content ? (
        <details className={styles.fullText}>
          <summary>Mostrar texto completo almacenado</summary>
          <div>{ordinance.text_content}</div>
        </details>
      ) : null}

      {ordinance.document ? (
        <p className={styles.documentReference}>
          Documento vinculado: <strong>{ordinance.document.original_filename}</strong>
        </p>
      ) : null}

      <p className={styles.legalDisclaimer}>
        Esta ficha facilita la consulta interna y no sustituye la comprobación
        del boletín oficial ni el criterio de los servicios jurídicos.
      </p>
    </div>
  );
}
