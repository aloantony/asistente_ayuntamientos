"use client";
import type { MunicipalCollection } from "../../lib/municipalWorkspace";
import {
  shouldShowProjectsPanel,
  shouldShowRequirementsPanel,
} from "../../lib/session";

import { BookOpen, CircleAlert, ClipboardList, Database, FileStack, Hammer, MapPin, ShieldCheck } from "lucide-react";
import Link from "next/link";
import styles from "../MunicipalWorkspace.module.css";
import {
  userHasPermission,
  type MaintenanceOrder,
  type MunicipalAsset,
  type Ordinance,
  type User,
} from "../types";
import {
  NotConfigured,
  SectionHeading,
  withOrganization,
} from "./shared";

export function HojaDeRuta({
  user,
  ordinances,
  assets,
  maintenance,
  canViewMap,
  organizationId,
}: {
  user: User;
  ordinances: MunicipalCollection<Ordinance> | null;
  assets: MunicipalCollection<MunicipalAsset> | null;
  maintenance: MunicipalCollection<MaintenanceOrder> | null;
  canViewMap: boolean;
  organizationId: number;
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
            href: withOrganization("/mapa", organizationId),
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
            title: "iConcejo",
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
