"use client";
import type { MunicipalCollection } from "../../lib/municipalWorkspace";

import { BookOpen, ClipboardList, Users, Wrench } from "lucide-react";
import type { ReactNode } from "react";
import styles from "../MunicipalWorkspace.module.css";
import {
  type Ordinance,
  type Organization,
} from "../types";
import { SectionHeading } from "./shared";
import type { WorkspaceTab } from "./types";

export function InformacionMunicipio({
  organization,
  ordinances,
  canViewOrdinances,
  seriesSection,
  onTabChange,
}: {
  organization: Organization;
  ordinances: MunicipalCollection<Ordinance> | null;
  canViewOrdinances: boolean;
  /** Series del municipio (padrón, clima, viviendas), montadas igual. */
  seriesSection: ReactNode;
  onTabChange: (tab: WorkspaceTab) => void;
}) {
  return (
    <div className={styles.tabContent}>
      {seriesSection}

      <section className={styles.card}>
        <SectionHeading
          description="Accesos directos a la información que ya está conectada al backend."
          eyebrow="Operativa"
          title="Áreas municipales"
        />
        <div className={styles.areaGrid}>
          <button type="button" onClick={() => onTabChange("ordinances")}>
            <BookOpen aria-hidden="true" size={20} strokeWidth={1.6} />
            <span>
              <strong>Normativa</strong>
              <small>
                {canViewOrdinances && ordinances
                  ? `${ordinances.total} documentos registrados`
                  : "Consulta condicionada por permisos"}
              </small>
            </span>
          </button>
          <button type="button" onClick={() => onTabChange("administration")}>
            <Wrench aria-hidden="true" size={20} strokeWidth={1.6} />
            <span>
              <strong>Instalaciones</strong>
              <small>Inventario, mapa y mantenimiento</small>
            </span>
          </button>
          <button type="button" onClick={() => onTabChange("people")}>
            <Users aria-hidden="true" size={20} strokeWidth={1.6} />
            <span>
              <strong>Personal</strong>
              <small>{organization.users.length} personas en el directorio</small>
            </span>
          </button>
          <button type="button" onClick={() => onTabChange("roadmap")}>
            <ClipboardList aria-hidden="true" size={20} strokeWidth={1.6} />
            <span>
              <strong>Hoja de ruta</strong>
              <small>Planificación municipal y trabajo pendiente</small>
            </span>
          </button>
        </div>
      </section>
    </div>
  );
}
