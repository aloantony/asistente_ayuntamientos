// Piezas compartidas por las secciones de la pantalla del ayuntamiento:
// etiquetas de dominio, formateadores y los bloques visuales que todas ellas
// repiten (métrica, encabezado de bloque, estado vacío y estado de recurso).
// Se extraen de MunicipalWorkspace para que cada sección viva en su archivo y
// las siguientes puedan añadirse sin volver a tocar un módulo monolítico.

import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import styles from "../MunicipalWorkspace.module.css";
import type {
  MaintenanceOrder,
  Municipality,
  MunicipalAsset,
  User,
} from "../types";
import type { MunicipalCollection } from "../../lib/municipalWorkspace";
import type { MunicipalContext, ResourceErrors } from "./types";

export const EMPTY_RESOURCE_ERRORS: ResourceErrors = {
  ordinances: "",
  assets: "",
  maintenance: "",
};

export const MUNICIPALITY_TYPE_LABELS: Record<
  Municipality["municipality_type"],
  string
> = {
  municipality: "Municipio",
  minor_local_entity: "Entidad local menor",
  district: "Distrito",
  other: "Otro",
};

export const RURAL_URBAN_PROFILE_LABELS: Record<
  Municipality["rural_urban_profile"],
  string
> = {
  rural: "Rural",
  semi_rural: "Semirrural",
  urban: "Urbano",
  mixed: "Mixto",
  unknown: "Sin clasificar",
};

export const ASSET_STATUS_LABELS: Record<MunicipalAsset["status"], string> = {
  active: "Activo",
  inactive: "Inactivo",
  retired: "Retirado",
  archived: "Archivado",
};

export const ASSET_CONDITION_LABELS: Record<
  MunicipalAsset["condition_status"],
  string
> = {
  good: "Buen estado",
  fair: "Estado regular",
  poor: "Requiere atención",
  unknown: "Sin revisar",
};

export const MAINTENANCE_STATUS_LABELS: Record<
  MaintenanceOrder["status"],
  string
> = {
  planned: "Planificada",
  scheduled: "Programada",
  in_progress: "En curso",
  completed: "Completada",
  cancelled: "Cancelada",
};

export const MAINTENANCE_PRIORITY_LABELS: Record<
  MaintenanceOrder["priority"],
  string
> = {
  low: "Baja",
  normal: "Normal",
  high: "Alta",
  urgent: "Urgente",
};

export const ORDINANCE_MANAGEMENT_PERMISSIONS = [
  "ordinances.create",
  "ordinances.edit",
  "ordinances.archive",
  "ordinances.import",
  "ordinances.review",
  "ordinances.manage",
];

export function getMunicipalContexts(user: User) {
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

export function formatInteger(value: number | null) {
  return value === null
    ? "No consta"
    : new Intl.NumberFormat("es-ES", { maximumFractionDigits: 0 }).format(
        value,
      );
}

export function formatDecimal(value: number | null, suffix: string) {
  if (value === null) {
    return "No consta";
  }
  return `${new Intl.NumberFormat("es-ES", {
    maximumFractionDigits: 2,
  }).format(value)} ${suffix}`;
}

export function formatDate(value: string | null) {
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

export function formatUpdatedAt(value: string) {
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

export function getInitials(value: string) {
  const initials = value
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("");
  return initials || "AY";
}

export function withOrganization(path: string, organizationId: number) {
  const separator = path.includes("?") ? "&" : "?";
  return `${path}${separator}organization_id=${organizationId}`;
}

export function permissionValue<T>(
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

export function Metric({
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

export function SectionHeading({
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

export function NotConfigured({
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

export function ResourceState({
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
    <div
      className={styles.resourceState}
      data-tone={tone}
      role={tone === "error" ? "alert" : undefined}
    >
      <Icon aria-hidden="true" size={24} strokeWidth={1.6} />
      <div>
        <h3>{title}</h3>
        <p>{description}</p>
        {action ? <div className={styles.resourceStateAction}>{action}</div> : null}
      </div>
    </div>
  );
}
