"use client";

import { Archive, Landmark, ShieldCheck } from "lucide-react";
import { SectionShell } from "./SectionShell";
import { ResourceState } from "./shared";
import styles from "./Administracion.module.css";
import type {
  ArchiveItem,
  ConservationState,
  DigitisationState,
  HeritageAsset,
} from "../types";

const KIND_LABELS: Record<HeritageAsset["kind"], string> = {
  building: "Edificio",
  archaeological: "Yacimiento",
  natural: "Natural",
  movable: "Bien mueble",
  intangible: "Inmaterial",
  other: "Otro",
};

const PROTECTION_LABELS: Record<HeritageAsset["protection_level"], string> = {
  none: "Sin declarar",
  local: "Protección local",
  regional: "Protección autonómica",
  bic: "Bien de Interés Cultural",
  unesco: "Patrimonio Mundial",
};

const CONSERVATION_LABELS: Record<ConservationState, string> = {
  good: "Buen estado",
  fair: "Estado regular",
  poor: "Requiere intervención",
  ruin: "En ruina",
  unknown: "Sin evaluar",
};

const ARCHIVE_KIND_LABELS: Record<ArchiveItem["kind"], string> = {
  document: "Documento",
  photograph: "Fotografía",
  map: "Plano",
  book: "Libro",
  audio: "Audio",
  video: "Vídeo",
  other: "Otro",
};

const DIGITISATION_LABELS: Record<DigitisationState, string> = {
  not_digitised: "Sin digitalizar",
  in_progress: "Digitalizando",
  digitised: "Digitalizado",
};

/**
 * Periodo de una pieza del archivo. De una caja se conoce el tramo de años,
 * casi nunca el día, y «sin fechar» es una respuesta honesta que la ficha debe
 * poder dar sin fingir un intervalo.
 */
export function describePeriod(item: ArchiveItem) {
  if (item.start_year === null && item.end_year === null) {
    return "Sin fechar";
  }
  if (item.start_year !== null && item.end_year !== null) {
    return item.start_year === item.end_year
      ? String(item.start_year)
      : `${item.start_year}–${item.end_year}`;
  }
  return String(item.start_year ?? item.end_year);
}

export function Patrimonio({
  assets,
  archive,
  canView,
}: {
  assets: HeritageAsset[];
  archive: ArchiveItem[];
  canView: boolean;
}) {
  return (
    <SectionShell
      count={canView ? assets.length + archive.length : null}
      icon={Landmark}
      sectionKey="patrimonio"
      title="Patrimonio y archivo"
    >
      {!canView ? (
        <ResourceState
          description="Tu cuenta no dispone del permiso heritage.view en esta organización."
          icon={ShieldCheck}
          title="Patrimonio no autorizado"
          tone="restricted"
        />
      ) : assets.length === 0 && archive.length === 0 ? (
        <ResourceState
          description="Todavía no se ha catalogado ningún bien patrimonial ni pieza de archivo."
          icon={Landmark}
          title="Sin catalogar"
        />
      ) : (
        <div className={styles.blocks}>
          {assets.length > 0 ? (
            <section className={styles.block}>
              <h3>
                <Landmark aria-hidden="true" size={15} strokeWidth={1.7} />
                Bienes patrimoniales
              </h3>
              <ul className={styles.rows}>
                {assets.map((asset) => (
                  <li key={asset.id}>
                    <div>
                      <strong>{asset.name}</strong>
                      <small>
                        {KIND_LABELS[asset.kind]}
                        {asset.period ? ` · ${asset.period}` : ""}
                      </small>
                    </div>
                    <span
                      className={styles.badge}
                      data-tone={
                        asset.protection_level === "none" ? undefined : "granted"
                      }
                    >
                      {PROTECTION_LABELS[asset.protection_level]}
                    </span>
                    <span className={styles.date}>
                      {CONSERVATION_LABELS[asset.conservation_state]}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          {archive.length > 0 ? (
            <section className={styles.block}>
              <h3>
                <Archive aria-hidden="true" size={15} strokeWidth={1.7} />
                Archivo municipal
              </h3>
              <ul className={styles.rows}>
                {archive.map((item) => (
                  <li key={item.id}>
                    <div>
                      <strong>{item.title}</strong>
                      <small>
                        {ARCHIVE_KIND_LABELS[item.kind]} · {item.reference}
                        {/* Lo que más se busca de un archivo de pueblo es
                            dónde está el papel. */}
                        {item.physical_location
                          ? ` · ${item.physical_location}`
                          : ""}
                      </small>
                    </div>
                    <span
                      className={styles.badge}
                      data-tone={
                        item.digitisation_state === "digitised"
                          ? "granted"
                          : undefined
                      }
                    >
                      {DIGITISATION_LABELS[item.digitisation_state]}
                    </span>
                    <span className={styles.date}>{describePeriod(item)}</span>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </div>
      )}
    </SectionShell>
  );
}
