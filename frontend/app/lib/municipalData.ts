import type {
  ClimateRecord,
  HouseholdStat,
  PadronRecord,
} from "../components/types";
import { adminRequestWithTotal } from "./api";

// Series del municipio (ADR-051). Se piden con el año más reciente primero,
// que es como las devuelve el backend.

export function fetchPadronSeries(organizationId: number, signal?: AbortSignal) {
  const params = new URLSearchParams({
    organization_id: String(organizationId),
    limit: "60",
  });

  return adminRequestWithTotal<PadronRecord[]>(
    `/municipal-data/padron?${params.toString()}`,
    "",
    "No se pudo cargar la serie de empadronamiento.",
    { signal },
  );
}

export function fetchClimateSeries(
  organizationId: number,
  referenceYear?: number,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    organization_id: String(organizationId),
    limit: "60",
  });
  if (referenceYear !== undefined) {
    params.set("reference_year", String(referenceYear));
  }

  return adminRequestWithTotal<ClimateRecord[]>(
    `/municipal-data/climate?${params.toString()}`,
    "",
    "No se pudo cargar la serie climática.",
    { signal },
  );
}

export function fetchHouseholdSeries(
  organizationId: number,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    organization_id: String(organizationId),
    limit: "60",
  });

  return adminRequestWithTotal<HouseholdStat[]>(
    `/municipal-data/households?${params.toString()}`,
    "",
    "No se pudo cargar el parque de viviendas.",
    { signal },
  );
}
