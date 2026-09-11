"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { adminRequest } from "../lib/api";
import type { MunicipalAsset } from "./types";
import styles from "./AssetInventory.module.css";

export function AssetDetailPanel({ assetId, organizationId, canViewMap, onEdit }: {
  assetId: number;
  organizationId: number;
  canViewMap: boolean;
  onEdit?: (asset: MunicipalAsset) => void;
}) {
  const [result, setResult] = useState<{ assetId: number; organizationId: number; asset?: MunicipalAsset; error?: string } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    void adminRequest<MunicipalAsset>(`/assets/${assetId}`, "", "No se pudo abrir la ficha.", { signal: controller.signal })
      .then((asset) => {
        if (controller.signal.aborted) return;
        setResult(asset.organization_id === organizationId
          ? { assetId, organizationId, asset }
          : { assetId, organizationId, error: "El elemento no pertenece a la organización seleccionada." });
      })
      .catch(() => {
        if (!controller.signal.aborted) setResult({ assetId, organizationId, error: "No se pudo abrir la ficha. Comprueba que el elemento existe y tienes acceso." });
      });
    return () => controller.abort();
  }, [assetId, organizationId]);
  const current = result?.assetId === assetId && result.organizationId === organizationId ? result : null;
  const asset = current?.asset;
  return <section className={styles.editor} aria-label="Ficha del elemento municipal">
    {!current ? <p role="status">Cargando ficha…</p> : current.error ? <p role="alert">{current.error}</p> : asset ? <>
      <div className={styles.editorHeading}>
        <div><p className={styles.eyebrow}>{asset.asset_type.category.name} · {asset.asset_type.name}</p><h2>{asset.name}</h2></div>
        {onEdit ? <button type="button" onClick={() => onEdit(asset)}>Editar elemento</button> : null}
      </div>
      <p>{asset.description || "Sin descripción."}</p>
      <dl>
        <dt>Código</dt><dd>{asset.code || "Sin código"}</dd>
        <dt>Material</dt><dd>{asset.material || "Sin informar"}</dd>
        <dt>Dimensiones</dt><dd>{asset.dimensions || "Sin informar"}</dd>
        <dt>Instalación</dt><dd>{asset.installed_on || "Sin fecha"}</dd>
        <dt>Última inspección</dt><dd>{asset.last_inspected_on || "Sin fecha"}</dd>
        <dt>Ubicación</dt><dd>{asset.location?.label || "Sin ubicar"}</dd>
        <dt>Notas</dt><dd>{asset.notes || "Sin notas"}</dd>
      </dl>
      {canViewMap ? <Link href={`/mapa?organization_id=${organizationId}&entity_type=asset&entity_id=${asset.id}`}>Abrir en el mapa</Link> : null}
    </> : null}
  </section>;
}
