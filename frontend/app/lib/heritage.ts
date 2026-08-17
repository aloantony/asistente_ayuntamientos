import type { ArchiveItem, HeritageAsset } from "../components/types";
import { adminRequestWithTotal } from "./api";

// Patrimonio y archivo municipal (ADR-045).

export function fetchHeritageAssets(
  organizationId: number,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    organization_id: String(organizationId),
    limit: "60",
  });

  return adminRequestWithTotal<HeritageAsset[]>(
    `/heritage/assets?${params.toString()}`,
    "",
    "No se pudo cargar el patrimonio municipal.",
    { signal },
  );
}

export function fetchArchiveItems(
  organizationId: number,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    organization_id: String(organizationId),
    limit: "60",
  });

  return adminRequestWithTotal<ArchiveItem[]>(
    `/heritage/archive?${params.toString()}`,
    "",
    "No se pudo cargar el archivo municipal.",
    { signal },
  );
}
