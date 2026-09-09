import type {
  GeoEntityType,
  GeoMapItem,
  MunicipalAsset,
} from "../components/types";
import { adminRequest, adminRequestWithTotal } from "./api";

const MAP_PAGE_SIZE = 500;

export function fetchGeoMapItems(params: URLSearchParams, signal?: AbortSignal) {
  const suffix = params.toString();
  return adminRequest<GeoMapItem[]>(
    `/geo/map-items${suffix ? `?${suffix}` : ""}`,
    "",
    "No se pudieron cargar los elementos del mapa.",
    { signal },
  );
}

export async function fetchAllGeoMapItems(
  params: URLSearchParams,
  signal?: AbortSignal,
) {
  const items: GeoMapItem[] = [];
  const seenItemKeys = new Set<string>();
  let offset = 0;

  while (true) {
    const pageParams = new URLSearchParams(params);
    pageParams.set("limit", String(MAP_PAGE_SIZE));
    pageParams.set("offset", String(offset));
    const page = await fetchGeoMapItems(pageParams, signal);
    let addedItems = 0;

    for (const item of page) {
      const itemKey = [
        item.entity_type,
        item.entity_id,
        item.role,
        item.location.id,
      ].join(":");
      if (!seenItemKeys.has(itemKey)) {
        seenItemKeys.add(itemKey);
        items.push(item);
        addedItems += 1;
      }
    }

    if (page.length < MAP_PAGE_SIZE) {
      return items;
    }
    if (addedItems === 0) {
      throw new Error("El servidor no pudo paginar los elementos del mapa.");
    }
    offset += page.length;
  }
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
      geometry?: { type: "LineString" | "Polygon"; coordinates: number[][] | number[][][] };
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
