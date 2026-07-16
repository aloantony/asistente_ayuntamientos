import type {
  Ordinance,
  OrdinanceComparison,
  OrdinanceLegalChunk,
  OrdinanceResultScope,
  OrdinanceSearchPage,
} from "../components/types";
import { adminRequest } from "./api";

export const ORDINANCE_LIBRARY_PAGE_SIZE = 12;

export type OrdinanceSearchFilters = {
  query: string;
  municipalityId?: number;
  municipalityName?: string;
  province?: string;
  topic?: string;
  strictTopic?: boolean;
  populationGte?: number;
  populationLt?: number;
  resultScope?: OrdinanceResultScope;
  includeInactive?: boolean;
  page?: number;
};

function addOptionalText(
  params: URLSearchParams,
  key: string,
  value: string | undefined,
) {
  const trimmed = value?.trim();
  if (trimmed) {
    params.set(key, trimmed);
  }
}

export function buildOrdinanceSearchPath(filters: OrdinanceSearchFilters) {
  const page = Math.max(1, filters.page ?? 1);
  const params = new URLSearchParams({
    q: filters.query.trim(),
    result_scope: filters.resultScope ?? "ordinances",
    limit: String(ORDINANCE_LIBRARY_PAGE_SIZE),
    offset: String((page - 1) * ORDINANCE_LIBRARY_PAGE_SIZE),
  });

  if (filters.municipalityId) {
    params.set("municipality_id", String(filters.municipalityId));
  }
  addOptionalText(params, "municipality_name", filters.municipalityName);
  addOptionalText(params, "province", filters.province);
  addOptionalText(params, "topic", filters.topic);
  if (filters.strictTopic) {
    params.set("strict_topic", "true");
  }
  if (filters.populationGte !== undefined) {
    params.set("population_gte", String(filters.populationGte));
  }
  if (filters.populationLt !== undefined) {
    params.set("population_lt", String(filters.populationLt));
  }
  if (filters.includeInactive) {
    params.set("include_inactive", "true");
  }

  return `/ordinances/search?${params.toString()}`;
}

export function fetchOrdinanceSearch(
  filters: OrdinanceSearchFilters,
  signal?: AbortSignal,
) {
  return adminRequest<OrdinanceSearchPage>(
    buildOrdinanceSearchPath(filters),
    "",
    "No se pudo consultar el repositorio de ordenanzas.",
    { signal },
  );
}

export async function fetchOrdinanceDetail(
  ordinanceId: number,
  options: { includeInactive?: boolean; signal?: AbortSignal } = {},
) {
  const detailParams = new URLSearchParams();
  if (options.includeInactive) {
    detailParams.set("include_inactive", "true");
  }
  const detailQuery = detailParams.toString();
  const [ordinance, chunks] = await Promise.all([
    adminRequest<Ordinance>(
      `/ordinances/${ordinanceId}${detailQuery ? `?${detailQuery}` : ""}`,
      "",
      "No se pudo cargar la ficha de la ordenanza.",
      { signal: options.signal },
    ),
    adminRequest<OrdinanceLegalChunk[]>(
      `/ordinances/${ordinanceId}/chunks${
        detailQuery ? `?${detailQuery}` : ""
      }`,
      "",
      "No se pudieron cargar los fragmentos revisados de la ordenanza.",
      { signal: options.signal },
    ),
  ]);

  return { ordinance, chunks };
}

export function fetchOrdinanceComparison(
  municipalityIds: number[],
  options: {
    topic?: string;
    includeInactive?: boolean;
    signal?: AbortSignal;
  } = {},
) {
  const params = new URLSearchParams();
  municipalityIds.forEach((municipalityId) => {
    params.append("municipality_ids", String(municipalityId));
  });
  addOptionalText(params, "topic", options.topic);
  if (options.includeInactive) {
    params.set("include_inactive", "true");
  }

  return adminRequest<OrdinanceComparison>(
    `/ordinances/comparison?${params.toString()}`,
    "",
    "No se pudo preparar la comparación municipal.",
    { signal: options.signal },
  );
}
