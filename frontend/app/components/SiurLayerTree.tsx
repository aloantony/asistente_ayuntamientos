"use client";

import type { ReactNode } from "react";
import {
  availableStylesForLayer,
  buildReferenceLegendUrl,
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
  disabled: "La capa está desactivada.",
  layer_missing: "La capa no figura en GetCapabilities.",
  license_not_approved: "La licencia todavía no tiene aprobación humana.",
  missing: "La capa ya no está presente en la fuente.",
  not_deliverable: "Este tipo de entrega aún no tiene renderizador.",
  service_mismatch: "El servicio no coincide con la evidencia verificada.",
  style_unsupported: "El estilo predeterminado no está verificado.",
  web_mercator_unsupported: "La capa no declara EPSG:3857 literal.",
};

function blockerLabel(blocker: string | null) {
  if (!blocker) {
    return null;
  }
  return BLOCKER_LABELS[blocker] ?? `Entrega no disponible (${blocker}).`;
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
            {blockerText ? (
              <p className="siur-layer-blocker" role="status">
                {blockerText}
              </p>
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
