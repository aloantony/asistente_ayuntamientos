"use client";
import type { MunicipalCollection } from "../../lib/municipalWorkspace";

import { CheckCircle2, CircleAlert, Database, ExternalLink, Hammer, MapPin, ShieldCheck } from "lucide-react";
import Link from "next/link";
import styles from "../MunicipalWorkspace.module.css";
import {
  type MaintenanceOrder,
  type MunicipalAsset,
} from "../types";
import {
  ASSET_CONDITION_LABELS,
  ASSET_STATUS_LABELS,
  MAINTENANCE_PRIORITY_LABELS,
  MAINTENANCE_STATUS_LABELS,
  Metric,
  ResourceState,
  SectionHeading,
  formatDate,
  permissionValue,
  withOrganization,
} from "./shared";
import type { ResourceErrors } from "./types";

export function ServiciosMunicipales({
  assets,
  maintenance,
  errors,
  canViewAssets,
  canViewMaintenance,
  canViewMap,
  organizationId,
  onRetry,
}: {
  assets: MunicipalCollection<MunicipalAsset> | null;
  maintenance: MunicipalCollection<MaintenanceOrder> | null;
  errors: ResourceErrors;
  canViewAssets: boolean;
  canViewMaintenance: boolean;
  canViewMap: boolean;
  organizationId: number;
  onRetry: () => void;
}) {
  const inventoryHref = withOrganization("/inventario", organizationId);
  const maintenanceHref = withOrganization("/mantenimiento", organizationId);
  const mapHref = withOrganization("/mapa", organizationId);

  return (
    <div className={styles.tabContent}>
      <SectionHeading
        actions={
          <>
            {canViewMap ? (
              <Link className={styles.secondaryAction} href={mapHref}>
                Abrir mapa
                <MapPin aria-hidden="true" size={15} />
              </Link>
            ) : null}
            {canViewAssets ? (
              <Link className={styles.primaryAction} href={inventoryHref}>
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
              <Link href={inventoryHref}>Ver todos</Link>
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
              action={
                <button
                  className={styles.secondaryAction}
                  onClick={onRetry}
                  type="button"
                >
                  Reintentar
                </button>
              }
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
                      href={
                        canViewMap && asset.location_id
                          ? withOrganization(
                              `/mapa?entity_type=asset&entity_id=${asset.id}`,
                              organizationId,
                            )
                          : inventoryHref
                      }
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
                  <Link className={styles.primaryAction} href={inventoryHref}>
                    Configurar inventario
                  </Link>
                  {canViewMap ? (
                    <Link className={styles.secondaryAction} href={mapHref}>
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
              <Link href={maintenanceHref}>Ver órdenes</Link>
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
              action={
                <button
                  className={styles.secondaryAction}
                  onClick={onRetry}
                  type="button"
                >
                  Reintentar
                </button>
              }
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
                <Link className={styles.primaryAction} href={maintenanceHref}>
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
