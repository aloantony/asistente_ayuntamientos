import type { GeoMapItem } from "../components/types";
import { adminRequest } from "./api";

export function fetchGeoMapItems(params: URLSearchParams) {
  const suffix = params.toString();
  return adminRequest<GeoMapItem[]>(
    `/geo/map-items${suffix ? `?${suffix}` : ""}`,
    "",
    "No se pudieron cargar los elementos del mapa.",
  );
}
