"use client";

import { UserRound, Users } from "lucide-react";
import styles from "../MunicipalWorkspace.module.css";
import {
  type Organization,
} from "../types";
import {
  NotConfigured,
  ResourceState,
  SectionHeading,
  getInitials,
} from "./shared";

export function Personal({ organization }: { organization: Organization }) {
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
