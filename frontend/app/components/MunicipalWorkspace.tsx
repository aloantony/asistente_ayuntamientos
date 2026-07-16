"use client";

import {
  BookOpen,
  Building2,
  CheckCircle2,
  CircleAlert,
  ClipboardList,
  CloudSun,
  Database,
  Droplets,
  ExternalLink,
  FileStack,
  FileText,
  Hammer,
  Landmark,
  MapPin,
  RefreshCw,
  ShieldCheck,
  UserRound,
  Users,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { fetchMunicipality } from "../lib/fetchers";
import {
  fetchMunicipalAssetSummary,
  fetchMunicipalMaintenanceSummary,
  fetchMunicipalOrdinances,
  fetchMunicipalOrganization,
  type MunicipalCollection,
} from "../lib/municipalWorkspace";
import { canViewMunicipalHub } from "../lib/permissions";
import {
  shouldShowProjectsPanel,
  shouldShowRequirementsPanel,
  useSession,
} from "../lib/session";
import styles from "./MunicipalWorkspace.module.css";
import {
  formatOrdinanceStatus,
  formatOrdinanceType,
  userHasPermission,
  type MaintenanceOrder,
  type Municipality,
  type MunicipalitySummary,
  type MunicipalAsset,
  type Organization,
  type OrganizationSummary,
  type Ordinance,
  type User,
} from "./types";

type WorkspaceTab =
  | "summary"
  | "ordinances"
  | "facilities"
  | "people"
  | "roadmap";

type MunicipalContext = {
  organization: OrganizationSummary;
  municipality: MunicipalitySummary;
};

type ResourceErrors = {
  ordinances: string;
  assets: string;
  maintenance: string;
};

type TabDefinition = {
  id: WorkspaceTab;
  label: string;
  icon: LucideIcon;
};

const TAB_DEFINITIONS: TabDefinition[] = [
  { id: "summary", label: "Resumen", icon: Landmark },
  { id: "ordinances", label: "Normativa", icon: BookOpen },
  { id: "facilities", label: "Instalaciones", icon: Wrench },
  { id: "people", label: "Personal", icon: Users },
  { id: "roadmap", label: "Hoja de ruta", icon: ClipboardList },
];

const EMPTY_RESOURCE_ERRORS: ResourceErrors = {
  ordinances: "",
  assets: "",
  maintenance: "",
};

const MUNICIPALITY_TYPE_LABELS: Record<
  Municipality["municipality_type"],
  string
> = {
  municipality: "Municipio",
  minor_local_entity: "Entidad local menor",
  district: "Distrito",
  other: "Otro",
};

const RURAL_URBAN_PROFILE_LABELS: Record<
  Municipality["rural_urban_profile"],
  string
> = {
  rural: "Rural",
  semi_rural: "Semirrural",
  urban: "Urbano",
  mixed: "Mixto",
  unknown: "Sin clasificar",
};

const ASSET_STATUS_LABELS: Record<MunicipalAsset["status"], string> = {
  active: "Activo",
  inactive: "Inactivo",
  retired: "Retirado",
  archived: "Archivado",
};

const ASSET_CONDITION_LABELS: Record<
  MunicipalAsset["condition_status"],
  string
> = {
  good: "Buen estado",
  fair: "Estado regular",
  poor: "Requiere atención",
  unknown: "Sin revisar",
};

const MAINTENANCE_STATUS_LABELS: Record<
  MaintenanceOrder["status"],
  string
> = {
  planned: "Planificada",
  scheduled: "Programada",
  in_progress: "En curso",
  completed: "Completada",
  cancelled: "Cancelada",
};

const MAINTENANCE_PRIORITY_LABELS: Record<
  MaintenanceOrder["priority"],
  string
> = {
  low: "Baja",
  normal: "Normal",
  high: "Alta",
  urgent: "Urgente",
};

const ORDINANCE_MANAGEMENT_PERMISSIONS = [
  "ordinances.create",
  "ordinances.edit",
  "ordinances.archive",
  "ordinances.import",
  "ordinances.review",
  "ordinances.manage",
];

function getMunicipalContexts(user: User) {
  return (user.organizations ?? [])
    .flatMap<MunicipalContext>((organization) => {
      if (organization.status === "archived" || !organization.municipality) {
        return [];
      }

      return [{ organization, municipality: organization.municipality }];
    })
    .sort((left, right) => {
      if (left.organization.status !== right.organization.status) {
        return left.organization.status === "active" ? -1 : 1;
      }
      return left.organization.name.localeCompare(
        right.organization.name,
        "es",
      );
    });
}

function formatInteger(value: number | null) {
  return value === null
    ? "No consta"
    : new Intl.NumberFormat("es-ES", { maximumFractionDigits: 0 }).format(
        value,
      );
}

function formatDecimal(value: number | null, suffix: string) {
  if (value === null) {
    return "No consta";
  }
  return `${new Intl.NumberFormat("es-ES", {
    maximumFractionDigits: 2,
  }).format(value)} ${suffix}`;
}

function formatDate(value: string | null) {
  if (!value) {
    return "Sin fecha";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "Sin fecha";
  }
  return new Intl.DateTimeFormat("es-ES", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    timeZone: "Europe/Madrid",
  }).format(date);
}

function formatUpdatedAt(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "Fecha no disponible";
  }
  return new Intl.DateTimeFormat("es-ES", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Europe/Madrid",
  }).format(date);
}

function getInitials(value: string) {
  const initials = value
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("");
  return initials || "AY";
}

function permissionValue<T>(
  canView: boolean,
  data: MunicipalCollection<T> | null,
  error: string,
) {
  if (!canView) {
    return "Sin acceso";
  }
  if (error) {
    return "No disponible";
  }
  return data?.total ?? "—";
}

function Metric({
  icon: Icon,
  label,
  value,
  detail,
}: {
  icon: LucideIcon;
  label: string;
  value: number | string;
  detail?: string;
}) {
  return (
    <div className={styles.metric}>
      <div className={styles.metricHeading}>
        <span>{label}</span>
        <Icon aria-hidden="true" size={18} strokeWidth={1.6} />
      </div>
      <strong className={typeof value === "number" ? undefined : styles.metricText}>
        {value}
      </strong>
      {detail ? <small>{detail}</small> : null}
    </div>
  );
}

function SectionHeading({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow: string;
  title: string;
  description: string;
  actions?: ReactNode;
}) {
  return (
    <header className={styles.sectionHeading}>
      <div>
        <p>{eyebrow}</p>
        <h2>{title}</h2>
        <span>{description}</span>
      </div>
      {actions ? <div className={styles.sectionActions}>{actions}</div> : null}
    </header>
  );
}

function NotConfigured({
  icon: Icon,
  title,
  description,
  wide = false,
}: {
  icon: LucideIcon;
  title: string;
  description: string;
  wide?: boolean;
}) {
  return (
    <article
      className={`${styles.notConfigured}${wide ? ` ${styles.notConfiguredWide}` : ""}`}
    >
      <span className={styles.notConfiguredIcon}>
        <Icon aria-hidden="true" size={20} strokeWidth={1.6} />
      </span>
      <div>
        <p>No configurado</p>
        <h3>{title}</h3>
        <span>{description}</span>
      </div>
    </article>
  );
}

function ResourceState({
  icon: Icon,
  title,
  description,
  tone = "empty",
  action,
}: {
  icon: LucideIcon;
  title: string;
  description: string;
  tone?: "empty" | "error" | "restricted";
  action?: ReactNode;
}) {
  return (
    <div className={styles.resourceState} data-tone={tone}>
      <Icon aria-hidden="true" size={24} strokeWidth={1.6} />
      <div>
        <h3>{title}</h3>
        <p>{description}</p>
        {action ? <div className={styles.resourceStateAction}>{action}</div> : null}
      </div>
    </div>
  );
}

function SummaryTab({
  municipality,
  organization,
  ordinances,
  assets,
  maintenance,
  errors,
  canViewOrdinances,
  canViewAssets,
  canViewMaintenance,
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
            description="No hay un registro validado de alcaldía, concejalías u órganos colegiados."
            icon={Building2}
            title="Gobierno y corporación"
          />
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

function OrdinancesTab({
  municipality,
  ordinances,
  error,
  canView,
  canManage,
}: {
  municipality: Municipality;
  ordinances: MunicipalCollection<Ordinance> | null;
  error: string;
  canView: boolean;
  canManage: boolean;
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

function FacilitiesTab({
  assets,
  maintenance,
  errors,
  canViewAssets,
  canViewMaintenance,
  canViewMap,
}: {
  assets: MunicipalCollection<MunicipalAsset> | null;
  maintenance: MunicipalCollection<MaintenanceOrder> | null;
  errors: ResourceErrors;
  canViewAssets: boolean;
  canViewMaintenance: boolean;
  canViewMap: boolean;
}) {
  return (
    <div className={styles.tabContent}>
      <SectionHeading
        actions={
          <>
            {canViewMap ? (
              <Link className={styles.secondaryAction} href="/mapa">
                Abrir mapa
                <MapPin aria-hidden="true" size={15} />
              </Link>
            ) : null}
            {canViewAssets ? (
              <Link className={styles.primaryAction} href="/inventario">
                Abrir inventario
                <ExternalLink aria-hidden="true" size={15} />
              </Link>
            ) : null}
          </>
        }
        description="Inventario municipal y órdenes de mantenimiento conectados a la organización seleccionada."
        eyebrow="Patrimonio operativo"
        title="Instalaciones"
      />

      <div className={styles.compactMetrics}>
        <Metric
          detail="Elementos no archivados"
          icon={Database}
          label="Activos registrados"
          value={permissionValue(canViewAssets, assets, errors.assets)}
        />
        <Metric
          detail="Planificadas, programadas o en curso"
          icon={Hammer}
          label="Órdenes abiertas"
          value={permissionValue(
            canViewMaintenance,
            maintenance,
            errors.maintenance,
          )}
        />
      </div>

      <div className={styles.facilitiesGrid}>
        <section className={styles.card}>
          <div className={styles.cardHeading}>
            <div>
              <p>Inventario</p>
              <h2>Activos municipales</h2>
            </div>
            {canViewAssets ? (
              <Link href="/inventario">Ver todos</Link>
            ) : null}
          </div>

          {!canViewAssets ? (
            <ResourceState
              description="Tu cuenta no dispone del permiso assets.view en esta organización."
              icon={ShieldCheck}
              title="Inventario no autorizado"
              tone="restricted"
            />
          ) : errors.assets ? (
            <ResourceState
              description={errors.assets}
              icon={CircleAlert}
              title="Inventario no disponible"
              tone="error"
            />
          ) : assets && assets.items.length > 0 ? (
            <div className={styles.assetList}>
              {assets.items.slice(0, 6).map((asset) => (
                <article key={asset.id}>
                  <span className={styles.assetIcon}>
                    <Database aria-hidden="true" size={17} strokeWidth={1.6} />
                  </span>
                  <div>
                    <Link
                      href={`/mapa?entity_type=asset&entity_id=${asset.id}`}
                    >
                      {asset.name}
                    </Link>
                    <p>
                      {asset.asset_type.category.name} · {asset.asset_type.name}
                    </p>
                    <div className={styles.itemMeta}>
                      <span>{ASSET_STATUS_LABELS[asset.status]}</span>
                      <span>{ASSET_CONDITION_LABELS[asset.condition_status]}</span>
                      {asset.code ? <span>{asset.code}</span> : null}
                    </div>
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <ResourceState
              action={
                <div className={styles.inlineActions}>
                  <Link className={styles.primaryAction} href="/inventario">
                    Configurar inventario
                  </Link>
                  {canViewMap ? (
                    <Link className={styles.secondaryAction} href="/mapa">
                      Revisar mapa
                    </Link>
                  ) : null}
                </div>
              }
              description="Todavía no se han registrado edificios, redes, mobiliario u otros activos. El módulo está listo para recibir el inventario real."
              icon={Database}
              title="Inventario municipal sin datos"
            />
          )}
        </section>

        <section className={styles.card}>
          <div className={styles.cardHeading}>
            <div>
              <p>Mantenimiento</p>
              <h2>Trabajo abierto</h2>
            </div>
            {canViewMaintenance ? (
              <Link href="/mantenimiento">Ver órdenes</Link>
            ) : null}
          </div>

          {!canViewMaintenance ? (
            <ResourceState
              description="Tu cuenta no dispone del permiso maintenance.view en esta organización."
              icon={ShieldCheck}
              title="Mantenimiento no autorizado"
              tone="restricted"
            />
          ) : errors.maintenance ? (
            <ResourceState
              description={errors.maintenance}
              icon={CircleAlert}
              title="Mantenimiento no disponible"
              tone="error"
            />
          ) : maintenance && maintenance.items.length > 0 ? (
            <div className={styles.maintenanceList}>
              {maintenance.items.slice(0, 6).map((order) => (
                <article key={order.id}>
                  <div className={styles.itemHeading}>
                    <div>
                      <span>{MAINTENANCE_PRIORITY_LABELS[order.priority]}</span>
                      <h3>{order.title}</h3>
                    </div>
                    <span
                      className={styles.statusPill}
                      data-tone={order.status}
                    >
                      {MAINTENANCE_STATUS_LABELS[order.status]}
                    </span>
                  </div>
                  <p>{order.asset.name}</p>
                  <div className={styles.itemMeta}>
                    <span>
                      {order.scheduled_for
                        ? `Prevista: ${formatDate(order.scheduled_for)}`
                        : "Pendiente de programar"}
                    </span>
                    {order.assigned_to ? (
                      <span>{order.assigned_to.full_name}</span>
                    ) : (
                      <span>Sin responsable</span>
                    )}
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <ResourceState
              action={
                <Link className={styles.primaryAction} href="/mantenimiento">
                  Abrir mantenimiento
                </Link>
              }
              description="No hay órdenes planificadas, programadas o en curso. Las nuevas actuaciones aparecerán aquí vinculadas a su activo."
              icon={CheckCircle2}
              title="Sin mantenimiento pendiente"
            />
          )}
        </section>
      </div>
    </div>
  );
}

function PeopleTab({ organization }: { organization: Organization }) {
  return (
    <div className={styles.tabContent}>
      <SectionHeading
        description="Personas con acceso a la organización municipal. Este directorio no presupone una relación laboral."
        eyebrow="Organización"
        title="Personal y colaboradores"
      />

      <section className={styles.card}>
        <div className={styles.cardHeading}>
          <div>
            <p>Directorio real</p>
            <h2>{organization.users.length} personas</h2>
          </div>
          <span className={styles.statusPill} data-tone={organization.status}>
            {organization.status === "active"
              ? "Organización activa"
              : organization.status === "paused"
                ? "Organización pausada"
                : "Organización archivada"}
          </span>
        </div>
        {organization.description ? (
          <p className={styles.organizationDescription}>
            {organization.description}
          </p>
        ) : null}

        {organization.users.length > 0 ? (
          <div className={styles.peopleGrid}>
            {organization.users.map((member) => (
              <article key={member.id}>
                <span className={styles.avatar} aria-hidden="true">
                  {getInitials(member.full_name)}
                </span>
                <div>
                  <h3>{member.full_name}</h3>
                  <a href={`mailto:${member.email}`}>{member.email}</a>
                  <div className={styles.itemMeta}>
                    <span>{member.is_active ? "Acceso activo" : "Acceso inactivo"}</span>
                    {member.is_superuser ? <span>Administración global</span> : null}
                  </div>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <ResourceState
            description="La organización no tiene personas asociadas en este momento."
            icon={Users}
            title="Directorio vacío"
          />
        )}
      </section>

      <NotConfigured
        description="Puestos, contratos, horarios, ausencias, productividad, facturas y expedientes laborales requieren un dominio de RR. HH. separado, con permisos y conservación específicos."
        icon={UserRound}
        title="Gestión de recursos humanos"
        wide
      />
    </div>
  );
}

function RoadmapTab({
  user,
  ordinances,
  assets,
  maintenance,
  canViewMap,
}: {
  user: User;
  ordinances: MunicipalCollection<Ordinance> | null;
  assets: MunicipalCollection<MunicipalAsset> | null;
  maintenance: MunicipalCollection<MaintenanceOrder> | null;
  canViewMap: boolean;
}) {
  const shortcuts = [
    ...(shouldShowRequirementsPanel(user)
      ? [
          {
            href: "/requisitos",
            title: "Necesidades",
            description: "Priorizar demandas y convertirlas en trabajo revisable.",
            icon: CircleAlert,
          },
        ]
      : []),
    ...(shouldShowProjectsPanel(user)
      ? [
          {
            href: "/proyectos",
            title: "Proyectos",
            description: "Seguir iniciativas, responsables y documentación.",
            icon: FileStack,
          },
        ]
      : []),
    ...(canViewMap
      ? [
          {
            href: "/mapa",
            title: "Mapa municipal",
            description: "Situar necesidades, proyectos y activos sobre el territorio.",
            icon: MapPin,
          },
        ]
      : []),
    ...(userHasPermission(user, "assistant.use")
      ? [
          {
            href: "/asistente",
            title: "Anacleto",
            description: "Preparar borradores y ordenar próximos pasos con supervisión.",
            icon: ShieldCheck,
          },
        ]
      : []),
  ];

  return (
    <div className={styles.tabContent}>
      <SectionHeading
        description="Una vista de planificación deberá reunir objetivos, hitos, responsables, dependencias y resultados aprobados."
        eyebrow="Planificación"
        title="Hoja de ruta municipal"
      />

      <NotConfigured
        description="No existe todavía una entidad persistente de hoja de ruta. Para evitar compromisos ficticios, esta vista no transforma automáticamente proyectos o necesidades en un plan aprobado."
        icon={ClipboardList}
        title="Plan municipal no configurado"
        wide
      />

      <section className={styles.card}>
        <SectionHeading
          description="Módulos operativos que ya contienen información trazable y pueden alimentar la futura planificación."
          eyebrow="Fuentes disponibles"
          title="Trabajo conectado"
        />
        <div className={styles.areaGrid}>
          {shortcuts.map(({ href, title, description, icon: Icon }) => (
            <Link href={href} key={href}>
              <Icon aria-hidden="true" size={20} strokeWidth={1.6} />
              <span>
                <strong>{title}</strong>
                <small>{description}</small>
              </span>
            </Link>
          ))}
        </div>
        <div className={styles.roadmapSignals}>
          <div>
            <BookOpen aria-hidden="true" size={17} />
            <span>Normas registradas</span>
            <strong>{ordinances?.total ?? "—"}</strong>
          </div>
          <div>
            <Database aria-hidden="true" size={17} />
            <span>Activos registrados</span>
            <strong>{assets?.total ?? "—"}</strong>
          </div>
          <div>
            <Hammer aria-hidden="true" size={17} />
            <span>Mantenimiento abierto</span>
            <strong>{maintenance?.total ?? "—"}</strong>
          </div>
        </div>
      </section>
    </div>
  );
}

function RestrictedState() {
  return (
    <section className={`panel ${styles.pageState}`}>
      <ShieldCheck aria-hidden="true" size={28} strokeWidth={1.6} />
      <p className="eyebrow">Ayuntamiento</p>
      <h1>Acceso restringido</h1>
      <p className="muted">Esta sección no está disponible para esta cuenta.</p>
    </section>
  );
}

function EmptyState({ user }: { user: User }) {
  const organizations = user.organizations ?? [];
  const hasEligibleOrganizations = organizations.some(
    (organization) => organization.status !== "archived",
  );
  const canManageOrganizations = userHasPermission(user, "organizations.manage");

  let message = "Tu cuenta todavía no pertenece a ninguna organización municipal.";
  if (organizations.length > 0 && !hasEligibleOrganizations) {
    message = "Las organizaciones afiliadas a tu cuenta están archivadas.";
  } else if (hasEligibleOrganizations) {
    message =
      "Tus organizaciones activas o pausadas no tienen un municipio vinculado.";
  }

  return (
    <section className={`panel ${styles.pageState}`}>
      <Building2 aria-hidden="true" size={28} strokeWidth={1.6} />
      <p className="eyebrow">Ayuntamiento</p>
      <h1>Sin municipio afiliado</h1>
      <p className="muted">{message}</p>
      {canManageOrganizations ? (
        <Link className={styles.primaryAction} href="/admin/organizaciones">
          Revisar organizaciones
        </Link>
      ) : null}
    </section>
  );
}

export function MunicipalWorkspace() {
  const { user, handleRequestError } = useSession();
  const [activeTab, setActiveTab] = useState<WorkspaceTab>("summary");
  const [selectedOrganizationId, setSelectedOrganizationId] = useState<
    number | null
  >(null);
  const [municipality, setMunicipality] = useState<Municipality | null>(null);
  const [organization, setOrganization] = useState<Organization | null>(null);
  const [ordinances, setOrdinances] = useState<
    MunicipalCollection<Ordinance> | null
  >(null);
  const [assets, setAssets] = useState<
    MunicipalCollection<MunicipalAsset> | null
  >(null);
  const [maintenance, setMaintenance] = useState<
    MunicipalCollection<MaintenanceOrder> | null
  >(null);
  const [resourceErrors, setResourceErrors] = useState<ResourceErrors>(
    EMPTY_RESOURCE_ERRORS,
  );
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState("");
  const [loadAttempt, setLoadAttempt] = useState(0);
  const requestSequenceRef = useRef(0);
  const tabButtonRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const contexts = user ? getMunicipalContexts(user) : [];
  const selectedContext =
    contexts.find(
      ({ organization: item }) => item.id === selectedOrganizationId,
    ) ??
    contexts[0] ??
    null;
  const permissionSignature = (user?.permissions ?? []).slice().sort().join(",");
  const canViewOrdinances = Boolean(
    user && userHasPermission(user, "ordinances.view"),
  );
  const canViewAssets = Boolean(user && userHasPermission(user, "assets.view"));
  const canViewMaintenance = Boolean(
    user && userHasPermission(user, "maintenance.view"),
  );
  const canViewMap = Boolean(
    user &&
      (userHasPermission(user, "map.view") ||
        userHasPermission(user, "map.manage")),
  );
  const canManageOrdinances = Boolean(
    user &&
      ORDINANCE_MANAGEMENT_PERMISSIONS.some((permission) =>
        userHasPermission(user, permission),
      ),
  );

  useEffect(() => {
    if (!user || !canViewMunicipalHub(user) || !selectedContext) {
      requestSequenceRef.current += 1;
      setMunicipality(null);
      setOrganization(null);
      setOrdinances(null);
      setAssets(null);
      setMaintenance(null);
      setError("");
      setResourceErrors(EMPTY_RESOURCE_ERRORS);
      setIsLoading(false);
      return;
    }

    const controller = new AbortController();
    const requestSequence = ++requestSequenceRef.current;
    const organizationId = selectedContext.organization.id;
    const municipalityId = selectedContext.municipality.id;

    setMunicipality(null);
    setOrganization(null);
    setOrdinances(null);
    setAssets(null);
    setMaintenance(null);
    setError("");
    setResourceErrors(EMPTY_RESOURCE_ERRORS);
    setIsLoading(true);

    async function loadWorkspace() {
      const [
        municipalityResult,
        organizationResult,
        ordinanceResult,
        assetResult,
        maintenanceResult,
      ] = await Promise.allSettled([
        fetchMunicipality(municipalityId, controller.signal),
        fetchMunicipalOrganization(organizationId, controller.signal),
        canViewOrdinances
          ? fetchMunicipalOrdinances(municipalityId, controller.signal)
          : Promise.resolve(null),
        canViewAssets
          ? fetchMunicipalAssetSummary(organizationId, controller.signal)
          : Promise.resolve(null),
        canViewMaintenance
          ? fetchMunicipalMaintenanceSummary(organizationId, controller.signal)
          : Promise.resolve(null),
      ] as const);

      if (
        controller.signal.aborted ||
        requestSequence !== requestSequenceRef.current
      ) {
        return;
      }

      if (municipalityResult.status === "fulfilled") {
        setMunicipality(municipalityResult.value);
      }
      if (organizationResult.status === "fulfilled") {
        setOrganization(organizationResult.value);
      }

      const essentialFailure =
        municipalityResult.status === "rejected"
          ? municipalityResult.reason
          : organizationResult.status === "rejected"
            ? organizationResult.reason
            : null;
      if (essentialFailure) {
        handleRequestError(
          essentialFailure,
          setError,
          "No se pudo cargar el espacio municipal.",
        );
      }

      if (ordinanceResult.status === "fulfilled") {
        setOrdinances(ordinanceResult.value);
      } else {
        handleRequestError(
          ordinanceResult.reason,
          (message) =>
            setResourceErrors((current) => ({
              ...current,
              ordinances: message,
            })),
          "No se pudo cargar la normativa municipal.",
        );
      }

      if (assetResult.status === "fulfilled") {
        setAssets(assetResult.value);
      } else {
        handleRequestError(
          assetResult.reason,
          (message) =>
            setResourceErrors((current) => ({ ...current, assets: message })),
          "No se pudo cargar el inventario municipal.",
        );
      }

      if (maintenanceResult.status === "fulfilled") {
        setMaintenance(maintenanceResult.value);
      } else {
        handleRequestError(
          maintenanceResult.reason,
          (message) =>
            setResourceErrors((current) => ({
              ...current,
              maintenance: message,
            })),
          "No se pudo cargar el mantenimiento municipal.",
        );
      }
    }

    void loadWorkspace().finally(() => {
      if (
        !controller.signal.aborted &&
        requestSequence === requestSequenceRef.current
      ) {
        setIsLoading(false);
      }
    });

    return () => controller.abort();
    // Las IDs y la firma de permisos delimitan la carga. El controlador y el
    // contador impiden que una respuesta antigua sustituya el municipio activo.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    user?.id,
    user?.is_superuser,
    selectedContext?.organization.id,
    selectedContext?.municipality.id,
    permissionSignature,
    loadAttempt,
  ]);

  if (!user) {
    return null;
  }
  if (!canViewMunicipalHub(user)) {
    return <RestrictedState />;
  }
  if (!selectedContext) {
    return <EmptyState user={user} />;
  }

  const isPaused = selectedContext.organization.status === "paused";
  const activeTabDefinition =
    TAB_DEFINITIONS.find((tab) => tab.id === activeTab) ?? TAB_DEFINITIONS[0];

  function handleTabKeyDown(
    event: KeyboardEvent<HTMLButtonElement>,
    currentIndex: number,
  ) {
    let nextIndex: number | null = null;

    if (event.key === "ArrowRight") {
      nextIndex = (currentIndex + 1) % TAB_DEFINITIONS.length;
    } else if (event.key === "ArrowLeft") {
      nextIndex =
        (currentIndex - 1 + TAB_DEFINITIONS.length) % TAB_DEFINITIONS.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = TAB_DEFINITIONS.length - 1;
    }

    if (nextIndex === null) {
      return;
    }

    event.preventDefault();
    setActiveTab(TAB_DEFINITIONS[nextIndex].id);
    tabButtonRefs.current[nextIndex]?.focus();
  }

  return (
    <section className={styles.workspace}>
      <header className={styles.masthead}>
        <div className={styles.identity}>
          <span className={styles.municipalityMark} aria-hidden="true">
            {getInitials(selectedContext.municipality.name)}
          </span>
          <div>
            <p>Espacio municipal</p>
            <h1>{selectedContext.municipality.name}</h1>
            <span>
              {selectedContext.municipality.province} ·{" "}
              {selectedContext.municipality.autonomous_community}
            </span>
          </div>
        </div>

        <div className={styles.contextPanel}>
          <span>Organización activa</span>
          {contexts.length > 1 ? (
            <select
              aria-label="Organización y municipio"
              onChange={(event) => {
                setSelectedOrganizationId(Number(event.target.value));
                setActiveTab("summary");
              }}
              value={selectedContext.organization.id}
            >
              {contexts.map(({ organization: item, municipality: summary }) => (
                <option key={item.id} value={item.id}>
                  {item.name} · {summary.name}
                  {item.status === "paused" ? " (pausada)" : ""}
                </option>
              ))}
            </select>
          ) : (
            <strong>{selectedContext.organization.name}</strong>
          )}
          <small>
            {isPaused ? "Modo de consulta · organización pausada" : "Datos en producción"}
          </small>
        </div>
      </header>

      {isPaused ? (
        <p className={styles.warning} role="status">
          La organización está pausada. La información permanece disponible en
          modo de consulta.
        </p>
      ) : null}

      <nav aria-label="Áreas del ayuntamiento" className={styles.tabs} role="tablist">
        {TAB_DEFINITIONS.map(({ id, label, icon: Icon }, index) => (
          <button
            aria-controls={`municipal-panel-${id}`}
            aria-selected={activeTab === id}
            id={`municipal-tab-${id}`}
            key={id}
            onClick={() => setActiveTab(id)}
            onKeyDown={(event) => handleTabKeyDown(event, index)}
            ref={(element) => {
              tabButtonRefs.current[index] = element;
            }}
            role="tab"
            tabIndex={activeTab === id ? 0 : -1}
            type="button"
          >
            <Icon aria-hidden="true" size={17} strokeWidth={1.6} />
            <span>{label}</span>
          </button>
        ))}
      </nav>

      <div
        aria-labelledby={`municipal-tab-${activeTabDefinition.id}`}
        id={`municipal-panel-${activeTabDefinition.id}`}
        role="tabpanel"
      >
        {isLoading ? (
          <div aria-busy="true" className={styles.loadingState}>
            <RefreshCw aria-hidden="true" size={22} />
            <div>
              <strong>Cargando el espacio municipal</strong>
              <span>Consultando fuentes autorizadas…</span>
            </div>
          </div>
        ) : error || !municipality || !organization ? (
          <ResourceState
            action={
              <button
                className={styles.primaryAction}
                onClick={() => setLoadAttempt((value) => value + 1)}
                type="button"
              >
                Reintentar
              </button>
            }
            description={error || "La ficha municipal está incompleta."}
            icon={CircleAlert}
            title="No se pudo abrir el espacio municipal"
            tone="error"
          />
        ) : activeTab === "summary" ? (
          <SummaryTab
            assets={assets}
            canViewAssets={canViewAssets}
            canViewMaintenance={canViewMaintenance}
            canViewOrdinances={canViewOrdinances}
            errors={resourceErrors}
            maintenance={maintenance}
            municipality={municipality}
            onTabChange={setActiveTab}
            ordinances={ordinances}
            organization={organization}
          />
        ) : activeTab === "ordinances" ? (
          <OrdinancesTab
            canManage={canManageOrdinances}
            canView={canViewOrdinances}
            error={resourceErrors.ordinances}
            municipality={municipality}
            ordinances={ordinances}
          />
        ) : activeTab === "facilities" ? (
          <FacilitiesTab
            assets={assets}
            canViewAssets={canViewAssets}
            canViewMaintenance={canViewMaintenance}
            canViewMap={canViewMap}
            errors={resourceErrors}
            maintenance={maintenance}
          />
        ) : activeTab === "people" ? (
          <PeopleTab organization={organization} />
        ) : (
          <RoadmapTab
            assets={assets}
            canViewMap={canViewMap}
            maintenance={maintenance}
            ordinances={ordinances}
            user={user}
          />
        )}
      </div>
    </section>
  );
}
