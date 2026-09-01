"use client";
import type { MunicipalCollection } from "../../lib/municipalWorkspace";

import { BookOpen, ClipboardList, CloudSun, Droplets, Users, Wrench } from "lucide-react";
import type { ReactNode } from "react";
import styles from "../MunicipalWorkspace.module.css";
import {
  type Ordinance,
  type Organization,
} from "../types";
import { NotConfigured, SectionHeading } from "./shared";
import type { WorkspaceTab } from "./types";

export function InformacionMunicipio({
  organization,
  ordinances,
  canViewOrdinances,
  governmentSection,
  seriesSection,
  onTabChange,
}: {
  organization: Organization;
  ordinances: MunicipalCollection<Ordinance> | null;
  canViewOrdinances: boolean;
  /** Corporación municipal; llega montada para no acoplar la ficha al dominio. */
  governmentSection: ReactNode;
  /** Series del municipio (padrón, clima, viviendas), montadas igual. */
  seriesSection: ReactNode;
  onTabChange: (tab: WorkspaceTab) => void;
}) {
  return (
    <div className={styles.tabContent}>
      {governmentSection}

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

      <section>
        <SectionHeading
          description="El diseño contempla estas áreas, pero todavía no existe un modelo persistente que permita mostrarlas con garantías."
          eyebrow="Siguiente capa de datos"
          title="Pendiente de configuración"
        />
        <div className={styles.notConfiguredGrid}>
          <NotConfigured
            description="No existe una serie meteorológica municipal conectada y trazable."
            icon={CloudSun}
            title="Clima"
          />
          <NotConfigured
            description="Analíticas, depósitos y red de abastecimiento requieren un módulo propio."
            icon={Droplets}
            title="Agua"
          />
        </div>
      </section>
    </div>
  );
}
