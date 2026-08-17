"use client";
import type { MunicipalCollection } from "../../lib/municipalWorkspace";

import { BookOpen, ClipboardList, CloudSun, Database, Droplets, Hammer, MapPin, UserRound, Users, Wrench } from "lucide-react";
import type { ReactNode } from "react";
import styles from "../MunicipalWorkspace.module.css";
import {
  type MaintenanceOrder,
  type MunicipalAsset,
  type Municipality,
  type Ordinance,
  type Organization,
} from "../types";
import {
  MUNICIPALITY_TYPE_LABELS,
  Metric,
  NotConfigured,
  RURAL_URBAN_PROFILE_LABELS,
  SectionHeading,
  formatDecimal,
  formatInteger,
  formatUpdatedAt,
  permissionValue,
} from "./shared";
import type { ResourceErrors, WorkspaceTab } from "./types";

export function InformacionMunicipio({
  municipality,
  organization,
  ordinances,
  assets,
  maintenance,
  errors,
  canViewOrdinances,
  canViewAssets,
  canViewMaintenance,
  governmentSection,
  seriesSection,
  administrationSection,
  onTabChange,
}: {
  municipality: Municipality;
  organization: Organization;
  ordinances: MunicipalCollection<Ordinance> | null;
  assets: MunicipalCollection<MunicipalAsset> | null;
  maintenance: MunicipalCollection<MaintenanceOrder> | null;
  errors: ResourceErrors;
  canViewOrdinances: boolean;
  canViewAssets: boolean;
  canViewMaintenance: boolean;
  /** Corporación municipal; llega montada para no acoplar la ficha al dominio. */
  governmentSection: ReactNode;
  /** Series del municipio (padrón, clima, viviendas), montadas igual. */
  seriesSection: ReactNode;
  /** Administración y comunicación municipal, montadas igual. */
  administrationSection: ReactNode;
  onTabChange: (tab: WorkspaceTab) => void;
}) {
  return (
    <div className={styles.tabContent}>
      <div className={styles.metricsGrid}>
        <Metric
          detail="Padrón registrado en la ficha"
          icon={Users}
          label="Población"
          value={formatInteger(municipality.population)}
        />
        <Metric
          detail={formatDecimal(municipality.surface_km2, "km²")}
          icon={MapPin}
          label="Densidad"
          value={formatDecimal(municipality.density, "hab./km²")}
        />
        <Metric
          detail="Repositorio municipal"
          icon={BookOpen}
          label="Normas"
          value={permissionValue(canViewOrdinances, ordinances, errors.ordinances)}
        />
        <Metric
          detail="Inventario no archivado"
          icon={Database}
          label="Activos"
          value={permissionValue(canViewAssets, assets, errors.assets)}
        />
        <Metric
          detail="Órdenes abiertas"
          icon={Hammer}
          label="Mantenimiento"
          value={permissionValue(
            canViewMaintenance,
            maintenance,
            errors.maintenance,
          )}
        />
        <Metric
          detail="Directorio de la organización"
          icon={UserRound}
          label="Personas"
          value={organization.users.length}
        />
      </div>

      <div className={styles.summaryGrid}>
        <article className={styles.card}>
          <div className={styles.cardHeading}>
            <div>
              <p>Ficha oficial</p>
              <h2>Identidad municipal</h2>
            </div>
            <span className={styles.statusPill} data-tone={municipality.status}>
              {municipality.status === "active" ? "Activa" : "Archivada"}
            </span>
          </div>
          <dl className={styles.definitionGrid}>
            <div>
              <dt>Código INE</dt>
              <dd>{municipality.ine_code ?? "No consta"}</dd>
            </div>
            <div>
              <dt>Código postal</dt>
              <dd>{municipality.postal_codes ?? "No consta"}</dd>
            </div>
            <div>
              <dt>Tipo</dt>
              <dd>{MUNICIPALITY_TYPE_LABELS[municipality.municipality_type]}</dd>
            </div>
            <div>
              <dt>Perfil territorial</dt>
              <dd>
                {RURAL_URBAN_PROFILE_LABELS[municipality.rural_urban_profile]}
              </dd>
            </div>
            <div>
              <dt>Provincia</dt>
              <dd>{municipality.province}</dd>
            </div>
            <div>
              <dt>Comunidad autónoma</dt>
              <dd>{municipality.autonomous_community}</dd>
            </div>
          </dl>
          <p className={styles.updatedAt}>
            Actualizada el {formatUpdatedAt(municipality.updated_at)}
          </p>
        </article>

        <article className={styles.card}>
          <div className={styles.cardHeading}>
            <div>
              <p>Contexto</p>
              <h2>Perfiles municipales</h2>
            </div>
          </div>
          <dl className={styles.profileList}>
            <div>
              <dt>Actividad económica</dt>
              <dd>{municipality.economic_profile ?? "No consta información."}</dd>
            </div>
            <div>
              <dt>Turismo</dt>
              <dd>{municipality.tourism_profile ?? "No consta información."}</dd>
            </div>
            <div>
              <dt>Geografía</dt>
              <dd>{municipality.geographic_notes ?? "No consta información."}</dd>
            </div>
            <div>
              <dt>Administración</dt>
              <dd>
                {municipality.administrative_notes ?? "No consta información."}
              </dd>
            </div>
          </dl>
        </article>
      </div>

      {governmentSection}

      {seriesSection}

      {administrationSection}

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
          <button type="button" onClick={() => onTabChange("facilities")}>
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
