import type {
  GeoEntityType,
  GeoMapItem,
  MunicipalAsset,
} from "../components/types";
import { adminRequest, adminRequestWithTotal } from "./api";

export function fetchGeoMapItems(params: URLSearchParams, signal?: AbortSignal) {
  const suffix = params.toString();
  return adminRequest<GeoMapItem[]>(
    `/geo/map-items${suffix ? `?${suffix}` : ""}`,
    "",
    "No se pudieron cargar los elementos del mapa.",
    { signal },
  );
}

export function fetchMunicipalAssets(
  organizationId: number,
  search: string,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    organization_id: String(organizationId),
    include_archived: "false",
    limit: "200",
  });
  if (search.trim()) {
    params.set("q", search.trim());
  }
  return adminRequestWithTotal<MunicipalAsset[]>(
    `/assets?${params.toString()}`,
    "",
    "No se pudo cargar el inventario municipal.",
    { signal },
  );
}

export function createEntityLocation(
  accessToken: string,
  payload: {
    entity_type: GeoEntityType;
    entity_id: number;
    role?: "primary" | "affected_area" | "reference";
    location: {
      organization_id?: number | null;
      municipality_id?: number | null;
      label: string;
      latitude: number;
      longitude: number;
      address_text?: string | null;
      place_name?: string | null;
      cadastral_reference?: string | null;
      source?:
        | "user_provided"
        | "assistant_extracted"
        | "geocoded"
        | "imported"
        | "manual_review";
      confidence?: number | null;
      review_status?: "draft" | "proposed" | "reviewed" | "rejected";
    };
  },
) {
  return adminRequest<GeoMapItem>(
    "/geo/entity-locations",
    accessToken,
    "No se pudo registrar la ubicación en el mapa.",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}
