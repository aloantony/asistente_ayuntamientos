"use client";

import type { ReactNode } from "react";
import {
  availableStylesForLayer,
  buildReferenceLegendUrl,
  buildReferenceMetadataUrl,
  referenceLayerBlocker,
  type ReferenceCatalog,
  type ReferenceLayer,
  type ReferenceLayerTreeNode,
  type SiurLayerControl,
  type SiurMapPreferences,
} from "../lib/referenceLayers";
import { SiurLegend } from "./SiurLegend";

type SiurLayerTreeProps = {
  catalog: ReferenceCatalog | null;
  error: string;
  isLoading: boolean;
  preferences: SiurMapPreferences | null;
  structuralWarnings: string[];
  tree: ReferenceLayerTreeNode[];
  onControlChange: (layerId: number, control: SiurLayerControl) => void;
  onMove: (layerId: number, direction: "forward" | "backward") => void;
};

const BLOCKER_LABELS: Record<string, string> = {
  attestation_missing: "Falta la evidencia técnica vigente.",
  bounds_invalid: "Los límites geográficos no son válidos.",
  catalog_stale: "La capa no pertenece a la instantánea actual.",
  catalog_service_missing: "La capa no tiene un servicio vigente en el catálogo.",
  composition_dependency_blocked:
    "La composición depende de una capa que todavía está bloqueada.",
  composition_dependency_cycle:
    "La composición contiene un ciclo entre capas dependientes.",
  composition_dependency_invalid:
    "La composición no declara dependencias locales válidas.",
  composition_dependency_missing:
    "Falta una capa requerida por esta composición.",
  composition_not_materialized:
    "La composición local todavía no se ha materializado.",
  disabled: "La capa está desactivada.",
  layer_missing: "La capa no figura en GetCapabilities.",
  license_not_approved: "La licencia todavía no tiene aprobación humana.",
  local_delivery_not_active:
    "No hay una versión local validada y activa para servir esta capa.",
  local_disabled: "La réplica local de esta capa está desactivada.",
  local_identify_unavailable:
    "La versión local no permite consultar elementos en este punto.",
  local_legend_unavailable: "La versión local no incluye una leyenda validada.",
  local_not_ready: "La copia local todavía no está lista.",
  local_source_changed:
    "La definición de la fuente ha cambiado y requiere una nueva sincronización.",
  local_version_invalid: "La versión local activa no ha superado la validación.",
  missing: "La capa ya no está presente en la fuente.",
  migration_backfill_required:
    "Esta capa requiere recalcular su estrategia después de la migración.",
  mirror_authorization_invalid:
    "La revisión del espejo local no supera la comprobación de integridad.",
  mirror_authorization_missing:
    "Falta una revisión humana que autorice conservar y servir esta copia local.",
  mirror_authorization_restricted:
    "La revisión vigente no permite conservar o servir localmente esta capa.",
  mirror_authorization_source_changed:
    "La fuente u origen revisados han cambiado y requieren una nueva autorización.",
  not_deliverable: "Este tipo de entrega aún no tiene renderizador.",
  remote_proxy_disabled:
    "No hay copia local activa y el proxy remoto está desactivado.",
  reviewed_ortho_substitution_blocked:
    "La capa IGN revisada no demuestra equivalencia territorial y temporal suficiente.",
  reviewed_ortho_2021_blocked:
    "La entrega histórica de 2021 está bloqueada y no puede reactivarse ni servirse.",
  reviewed_ortho_legacy_fenced:
    "Los bytes locales activos no tienen la evidencia de identidad y paridad exigida.",
  service_mismatch: "El servicio no coincide con la evidencia verificada.",
  source_candidate_missing:
    "No se ha encontrado una fuente segura para replicar esta capa.",
  source_discovery_error:
    "No se pudo determinar una fuente local segura para esta capa.",
  source_target_unsupported:
    "La fuente encontrada aún no tiene una estrategia local compatible.",
  strategy_delivery_kind_mismatch:
    "La versión activa no coincide con la estrategia local aprobada.",
  strategy_missing: "Falta clasificar la estrategia local de esta capa.",
  style_coverage_incomplete:
    "La copia local no cubre todos los estilos vigentes de la capa.",
  style_unsupported:
    "La copia local no incluye uno de los estilos vigentes de la capa.",
  web_mercator_unsupported: "La capa no declara EPSG:3857 literal.",
};

function mirrorStatusLabel(layer: ReferenceLayer) {
  switch (layer.mirror_status) {
    case "active":
      return "Copia local activa.";
    case "syncing":
      return layer.active_version_id
        ? "Actualizando la copia local; la versión vigente sigue disponible."
        : "Sincronizando la copia local por primera vez.";
    case "serving_previous":
      return "La última actualización falló; se mantiene la versión local anterior.";
    case "pending":
      return "Copia local pendiente de sincronización.";
    case "error":
      return "La copia local no está disponible porque la sincronización falló.";
    case "disabled":
      return "Sincronización local desactivada.";
    case "legacy":
      return "Entrega heredada pendiente de incorporarse al espejo versionado.";
    case "not_applicable":
      return null;
  }
}

function blockerLabel(blocker: string | null) {
  if (!blocker) {
    return null;
  }
  return BLOCKER_LABELS[blocker] ?? `Entrega no disponible (${blocker}).`;
}

function sourceSubstitutionLabel(layer: ReferenceLayer) {
  switch (layer.source_substitution_status) {
    case "exact":
      return "Fuente oficial IGN elegible del mismo producto y año";
    case "substitute_degraded":
      return "Entrega local sustitutiva degradada";
    case "blocked":
      return "Sustitución IGN bloqueada";
    case "invalid":
      return "Evidencia de sustitución no válida";
    case null:
      return null;
  }
}

function collectLeafLayers(node: ReferenceLayerTreeNode): ReferenceLayer[] {
  if (node.layer.node_type === "layer") {
    return [node.layer];
  }
  return node.children.flatMap(collectLeafLayers);
}

export function SiurLayerTree({
  catalog,
  error,
  isLoading,
  onControlChange,
  onMove,
  preferences,
  structuralWarnings,
  tree,
}: SiurLayerTreeProps) {
  const renderNodes = (nodes: ReferenceLayerTreeNode[]): ReactNode => (
    <ul className="siur-layer-tree">
      {nodes.map((node) => {
        const { layer } = node;
        if (layer.node_type === "group") {
          const leafCount = collectLeafLayers(node).length;
          return (
            <li className="siur-layer-group" key={layer.id}>
              <details>
                <summary>
                  <span>{layer.title}</span>
                  <small>{leafCount}</small>
                </summary>
                {layer.description ? <p>{layer.description}</p> : null}
                {node.children.length > 0 ? renderNodes(node.children) : (
                  <p className="siur-layer-note">Grupo sin capas.</p>
                )}
              </details>
            </li>
          );
        }

        const control = preferences?.layers[String(layer.id)] ?? null;
        const styles = catalog ? availableStylesForLayer(catalog, layer) : [];
        const blocker = catalog ? referenceLayerBlocker(catalog, layer) : null;
        const blockerText = blockerLabel(blocker);
        const mirrorText = mirrorStatusLabel(layer);
        const substitutionLabel = sourceSubstitutionLabel(layer);
        const stackIndex = preferences?.stackOrder.indexOf(layer.id) ?? -1;
        const selectedStyle = styles.find(
          (style) => style.id === control?.styleId,
        );
        const legendAvailable =
          selectedStyle?.legend_available ??
          (styles.length === 0 && layer.legend_available);
        const legendUrl =
          legendAvailable && catalog?.organization_id
            ? buildReferenceLegendUrl(
                catalog.organization_id,
                layer.id,
                control?.styleId,
                layer.active_version_id,
                layer.active_generation,
              )
            : null;
        const metadataUrl =
          layer.metadata_available && catalog?.organization_id
            ? buildReferenceMetadataUrl(
                catalog.organization_id,
                layer.id,
              )
            : null;

        return (
          <li className="siur-layer-leaf" key={layer.id}>
            <div className="siur-layer-leaf-heading">
              <label>
                <input
                  checked={control?.visible ?? false}
                  disabled={!control || Boolean(blocker)}
                  onChange={(event) => {
                    if (control) {
                      onControlChange(layer.id, {
                        ...control,
                        visible: event.target.checked,
                      });
                    }
                  }}
                  type="checkbox"
                />
                <span>{layer.title}</span>
              </label>
              <div className="siur-layer-stack-buttons">
                <button
                  aria-label={`Enviar ${layer.title} detrás`}
                  disabled={!control || stackIndex <= 0}
                  onClick={() => onMove(layer.id, "backward")}
                  title="Enviar detrás"
                  type="button"
                >
                  ↓
                </button>
                <button
                  aria-label={`Traer ${layer.title} delante`}
                  disabled={
                    !control ||
                    !preferences ||
                    stackIndex === preferences.stackOrder.length - 1
                  }
                  onClick={() => onMove(layer.id, "forward")}
                  title="Traer delante"
                  type="button"
                >
                  ↑
                </button>
              </div>
            </div>
            {layer.description ? <p>{layer.description}</p> : null}
            {mirrorText ? (
              <p className="siur-layer-mirror-status" role="status">
                {mirrorText}
              </p>
            ) : null}
            {substitutionLabel && layer.source_substitution_notice ? (
              <p
                className={
                  "siur-layer-substitution " +
                  `siur-layer-substitution-${layer.source_substitution_status}`
                }
                role="status"
              >
                <strong>{substitutionLabel}</strong>
                <span>{layer.source_substitution_notice}</span>
                {layer.source_substitution_selected_layer ? (
                  <small>
                    Capa IGN: <code>{layer.source_substitution_selected_layer}</code>
                  </small>
                ) : null}
                {layer.source_substitution_scope ? (
                  <small>
                    {layer.source_substitution_scope === "active_delivery"
                      ? "Clasificación de los bytes locales activos."
                      : "Clasificación de la fuente candidata; todavía no describe una entrega activa."}
                  </small>
                ) : null}
                {layer.source_substitution_attribution ? (
                  <small>
                    Atribución obligatoria:{" "}
                    {layer.source_substitution_attribution}
                  </small>
                ) : null}
              </p>
            ) : null}
            {blockerText ? (
              <p className="siur-layer-blocker" role="status">
                <span>{blockerText}</span>
                <small className="siur-layer-blocker-code">
                  Motivo técnico: <code>{blocker}</code>
                </small>
              </p>
            ) : null}
            {metadataUrl ? (
              <a
                className="siur-layer-metadata-link"
                href={metadataUrl}
                rel="noopener noreferrer"
                target="_blank"
              >
                Metadatos
              </a>
            ) : null}
            {control && !blocker ? (
              <div className="siur-layer-controls">
                <label>
                  <span>Opacidad</span>
                  <output>{Math.round(control.opacity * 100)} %</output>
                  <input
                    aria-label={`Opacidad de ${layer.title}`}
                    max="1"
                    min="0"
                    onChange={(event) =>
                      onControlChange(layer.id, {
                        ...control,
                        opacity: Number(event.target.value),
                      })
                    }
                    step="0.05"
                    type="range"
                    value={control.opacity}
                  />
                </label>
                {styles.length > 0 ? (
                  <label>
                    <span>Estilo aprobado</span>
                    <select
                      aria-label={`Estilo aprobado de ${layer.title}`}
                      onChange={(event) =>
                        onControlChange(layer.id, {
                          ...control,
                          styleId: Number(event.target.value),
                        })
                      }
                      value={control.styleId ?? ""}
                    >
                      {styles.map((style) => (
                        <option key={style.id} value={style.id}>
                          {style.title}
                        </option>
                      ))}
                    </select>
                  </label>
                ) : null}
                {legendUrl ? (
                  <SiurLegend
                    key={legendUrl}
                    title={layer.title}
                    url={legendUrl}
                  />
                ) : null}
              </div>
            ) : null}
          </li>
        );
      })}
    </ul>
  );

  return (
    <section className="siur-layer-section" aria-labelledby="siur-layer-title">
      <div className="siur-layer-section-heading">
        <h2 id="siur-layer-title">Cartografía SIUR</h2>
        {catalog ? (
          <small title="Instantánea del catálogo">
            {catalog.layers.length} nodos
          </small>
        ) : null}
      </div>
      {isLoading ? (
        <p className="siur-layer-note" role="status">
          Cargando catálogo verificado…
        </p>
      ) : null}
      {error ? (
        <p className="siur-layer-error" role="alert">
          {error}
        </p>
      ) : null}
      {structuralWarnings.length > 0 ? (
        <div className="siur-layer-warning" role="status">
          <strong>Jerarquía recuperada con avisos</strong>
          <ul>
            {structuralWarnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {catalog && !error && tree.length > 0 ? renderNodes(tree) : null}
      {catalog && !error && tree.length === 0 ? (
        <p className="siur-layer-note">El catálogo actual no contiene nodos.</p>
      ) : null}
      {!catalog && !isLoading && !error ? (
        <p className="siur-layer-note">
          Selecciona una organización para cargar la cartografía SIUR.
        </p>
      ) : null}
    </section>
  );
}
