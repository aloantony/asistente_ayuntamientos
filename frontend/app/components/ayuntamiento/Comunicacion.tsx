"use client";

import { Megaphone, ShieldCheck } from "lucide-react";
import { SectionShell } from "./SectionShell";
import { ResourceState, formatDate } from "./shared";
import styles from "./Administracion.module.css";
import type { MunicipalNotice } from "../types";

const NOTICE_STATUS_LABELS: Record<MunicipalNotice["status"], string> = {
  draft: "Borrador",
  published: "Expuesto",
  withdrawn: "Retirado",
  expired: "Caducado",
};

const NOTICE_KIND_LABELS: Record<MunicipalNotice["kind"], string> = {
  bando: "Bando",
  edicto: "Edicto",
  convocatoria: "Convocatoria",
  other: "Anuncio",
};

/**
 * Un bando retirado conserva la fecha en que estuvo expuesto: puede tener que
 * demostrarse después, así que la pantalla la sigue mostrando en lugar de
 * dejar el hueco de un dato borrado.
 */
export function describeExposure(notice: MunicipalNotice) {
  if (notice.published_on === null) {
    return "Sin exponer";
  }
  const since = `Desde ${formatDate(notice.published_on)}`;
  if (notice.status === "withdrawn") {
    return `${since} · retirado`;
  }
  if (notice.expires_on) {
    return `${since} hasta ${formatDate(notice.expires_on)}`;
  }
  return since;
}

export function Comunicacion({
  notices,
  canView,
}: {
  notices: MunicipalNotice[];
  canView: boolean;
}) {
  return (
    <SectionShell
      count={canView ? notices.length : null}
      icon={Megaphone}
      sectionKey="comunicacion"
      title="Comunicación municipal"
    >
      {!canView ? (
        <ResourceState
          description="Tu cuenta no dispone del permiso communications.view en esta organización."
          icon={ShieldCheck}
          title="Comunicación no autorizada"
          tone="restricted"
        />
      ) : notices.length === 0 ? (
        <ResourceState
          description="Todavía no se ha redactado ningún bando ni edicto. Los que se publiquen quedarán aquí con su periodo de exposición."
          icon={Megaphone}
          title="Sin bandos registrados"
        />
      ) : (
        <ul className={styles.rows}>
          {notices.map((notice) => (
            <li key={notice.id}>
              <div>
                <strong>{notice.title}</strong>
                <small>{NOTICE_KIND_LABELS[notice.kind]}</small>
              </div>
              <span className={styles.badge} data-tone={notice.status}>
                {NOTICE_STATUS_LABELS[notice.status]}
              </span>
              <span className={styles.date}>{describeExposure(notice)}</span>
            </li>
          ))}
        </ul>
      )}
    </SectionShell>
  );
}
