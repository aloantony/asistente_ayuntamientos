"use client";
import type { MunicipalCollection } from "../../lib/municipalWorkspace";

import { CircleAlert, ExternalLink, FileStack, FileText, ShieldCheck } from "lucide-react";
import Link from "next/link";
import styles from "../MunicipalWorkspace.module.css";
import {
  formatOrdinanceStatus,
  formatOrdinanceType,
  type Municipality,
  type Ordinance,
} from "../types";
import {
  NotConfigured,
  ResourceState,
  SectionHeading,
  formatDate,
} from "./shared";

export function Normativa({
  municipality,
  ordinances,
  error,
  canView,
  canManage,
  onRetry,
}: {
  municipality: Municipality;
  ordinances: MunicipalCollection<Ordinance> | null;
  error: string;
  canView: boolean;
  canManage: boolean;
  onRetry: () => void;
}) {
  const adminHref = `/admin/ordenanzas?q=${encodeURIComponent(municipality.name)}`;

  return (
    <div className={styles.tabContent}>
      <SectionHeading
        actions={
          canManage ? (
            <Link className={styles.primaryAction} href={adminHref}>
              Gestionar repositorio
              <ExternalLink aria-hidden="true" size={15} />
            </Link>
          ) : undefined
        }
        description="Ordenanzas y reglamentos vinculados a la ficha municipal, con su fuente y estado de vigencia."
        eyebrow="Repositorio municipal"
        title="Normativa"
      />

      {!canView ? (
        <ResourceState
          description="Tu cuenta no dispone del permiso ordinances.view para consultar el repositorio."
          icon={ShieldCheck}
          title="Consulta no autorizada"
          tone="restricted"
        />
      ) : error ? (
        <ResourceState
          action={
            <button className={styles.secondaryAction} onClick={onRetry} type="button">
              Reintentar
            </button>
          }
          description={error}
          icon={CircleAlert}
          title="No se pudo cargar la normativa"
          tone="error"
        />
      ) : ordinances && ordinances.items.length > 0 ? (
        <section className={styles.collectionCard}>
          <div className={styles.collectionSummary}>
            <div>
              <strong>{ordinances.total}</strong>
              <span>documentos no archivados</span>
            </div>
            <p>
              Se muestran {ordinances.items.length} de {ordinances.total}.
            </p>
          </div>
          <div className={styles.ordinanceList}>
            {ordinances.items.map((ordinance) => (
              <article key={ordinance.id}>
                <div className={styles.ordinanceIcon}>
                  <FileText aria-hidden="true" size={20} strokeWidth={1.6} />
                </div>
                <div className={styles.ordinanceMain}>
                  <div className={styles.itemHeading}>
                    <div>
                      <span>{formatOrdinanceType(ordinance.ordinance_type)}</span>
                      <h3>{ordinance.title}</h3>
                    </div>
                    <span
                      className={styles.statusPill}
                      data-tone={ordinance.status}
                    >
                      {formatOrdinanceStatus(ordinance.status)}
                    </span>
                  </div>
                  <p>
                    {ordinance.summary ??
                      "El repositorio no incluye todavía un resumen de este documento."}
                  </p>
                  <div className={styles.itemMeta}>
                    <span>{ordinance.topic}</span>
                    {ordinance.subtopic ? <span>{ordinance.subtopic}</span> : null}
                    <span>
                      Publicación: {formatDate(ordinance.publication_date)}
                    </span>
                    {ordinance.official_bulletin ? (
                      <span>{ordinance.official_bulletin}</span>
                    ) : null}
                  </div>
                  {ordinance.source_url ? (
                    <a
                      href={ordinance.source_url}
                      rel="noreferrer noopener"
                      target="_blank"
                    >
                      Consultar fuente oficial
                      <ExternalLink aria-hidden="true" size={14} />
                    </a>
                  ) : (
                    <span className={styles.missingSource}>
                      Fuente oficial sin enlace registrado
                    </span>
                  )}
                </div>
              </article>
            ))}
          </div>
        </section>
      ) : (
        <ResourceState
          action={
            canManage ? (
              <Link className={styles.secondaryAction} href={adminHref}>
                Añadir normativa
              </Link>
            ) : undefined
          }
          description="El municipio no tiene ordenanzas activas registradas en el repositorio."
          icon={FileStack}
          title="Repositorio vacío"
        />
      )}

      <NotConfigured
        description="Noticias, bandos, actas, páginas informativas y documentos generales necesitan un CMS municipal con revisión y publicación. No se sustituyen por contenido de demostración."
        icon={FileStack}
        title="Biblioteca y CMS municipal"
        wide
      />
    </div>
  );
}

