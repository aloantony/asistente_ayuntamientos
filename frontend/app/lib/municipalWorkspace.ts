import type {
  MaintenanceOrder,
  MunicipalAsset,
  Organization,
  Ordinance,
} from "../components/types";
import { adminRequest, adminRequestWithTotal } from "./api";

export type MunicipalCollection<T> = {
  items: T[];
  total: number;
};

export function fetchMunicipalOrganization(
  organizationId: number,
  signal?: AbortSignal,
) {
  return adminRequest<Organization>(
    `/organizations/${organizationId}`,
    "",
    "No se pudo cargar el directorio de la organización.",
    { signal },
  );
}

export function fetchMunicipalOrdinances(
  municipalityId: number,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    municipality_id: String(municipalityId),
    include_archived: "false",
    limit: "200",
  });

  return adminRequestWithTotal<Ordinance[]>(
    `/ordinances?${params.toString()}`,
    "",
    "No se pudo cargar el repositorio normativo municipal.",
    { signal },
  );
}

export function fetchMunicipalAssetSummary(
  organizationId: number,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    organization_id: String(organizationId),
    include_archived: "false",
    limit: "12",
  });

  return adminRequestWithTotal<MunicipalAsset[]>(
    `/assets?${params.toString()}`,
    "",
    "No se pudo cargar el inventario municipal.",
    { signal },
  );
}

export function fetchMunicipalMaintenanceSummary(
  organizationId: number,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    organization_id: String(organizationId),
    include_closed: "false",
    limit: "12",
  });

  return adminRequestWithTotal<MaintenanceOrder[]>(
    `/maintenance/orders?${params.toString()}`,
    "",
    "No se pudo cargar el mantenimiento municipal.",
    { signal },
  );
}
