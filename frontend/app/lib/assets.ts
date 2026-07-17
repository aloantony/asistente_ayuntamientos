import type {
  AssetConditionStatus,
  AssetStatus,
  AssetTaxonomyStatus,
  MunicipalAsset,
  MunicipalAssetCategory,
  MunicipalAssetType,
} from "../components/types";
import { adminRequest, adminRequestWithTotal } from "./api";

const TAXONOMY_PAGE_SIZE = 200;

type AssetCategoryWrite = {
  organization_id: number;
  code: string;
  name: string;
  description: string | null;
  color: string | null;
  sort_order: number;
  status: AssetTaxonomyStatus;
};

type AssetTypeWrite = {
  organization_id: number;
  category_id: number;
  code: string;
  name: string;
  description: string | null;
  sort_order: number;
  status: AssetTaxonomyStatus;
};

export type MunicipalAssetWrite = {
  organization_id: number;
  asset_type_id: number;
  code: string | null;
  name: string;
  description: string | null;
  status: AssetStatus;
  condition_status: AssetConditionStatus;
  material: string | null;
  dimensions: string | null;
  installed_on: string | null;
  last_inspected_on: string | null;
  notes: string | null;
};

export type MunicipalAssetPatch = Partial<
  Omit<MunicipalAssetWrite, "organization_id">
>;

export type MunicipalAssetFilters = {
  query?: string;
  categoryId?: number;
  assetTypeId?: number;
  status?: AssetStatus;
  conditionStatus?: AssetConditionStatus;
  includeArchived?: boolean;
  limit?: number;
  offset?: number;
};

function taxonomyParams(organizationId: number, offset: number) {
  return new URLSearchParams({
    organization_id: String(organizationId),
    include_archived: "true",
    limit: String(TAXONOMY_PAGE_SIZE),
    offset: String(offset),
  });
}

async function fetchAllPages<T>(
  buildPath: (offset: number) => string,
  fallbackError: string,
  signal?: AbortSignal,
) {
  const items: T[] = [];
  let total = 0;

  do {
    const response = await adminRequestWithTotal<T[]>(
      buildPath(items.length),
      "",
      fallbackError,
      { signal },
    );
    items.push(...response.items);
    total = response.total;

    if (response.items.length === 0) {
      break;
    }
  } while (items.length < total);

  return { items, total };
}

export function fetchAssetCategories(
  organizationId: number,
  signal?: AbortSignal,
) {
  return fetchAllPages<MunicipalAssetCategory>(
    (offset) =>
      `/assets/categories?${taxonomyParams(organizationId, offset).toString()}`,
    "No se pudieron cargar las categorías del inventario.",
    signal,
  );
}

export function fetchAssetTypes(
  organizationId: number,
  signal?: AbortSignal,
) {
  return fetchAllPages<MunicipalAssetType>(
    (offset) =>
      `/assets/types?${taxonomyParams(organizationId, offset).toString()}`,
    "No se pudieron cargar los tipos del inventario.",
    signal,
  );
}

function assetParams(
  organizationId: number,
  filters: MunicipalAssetFilters,
) {
  const params = new URLSearchParams({
    organization_id: String(organizationId),
    limit: String(filters.limit ?? 40),
    offset: String(filters.offset ?? 0),
  });
  if (filters.query?.trim()) {
    params.set("q", filters.query.trim());
  }
  if (filters.categoryId) {
    params.set("category_id", String(filters.categoryId));
  }
  if (filters.assetTypeId) {
    params.set("asset_type_id", String(filters.assetTypeId));
  }
  if (filters.status) {
    params.set("status", filters.status);
  }
  if (filters.conditionStatus) {
    params.set("condition_status", filters.conditionStatus);
  }
  if (filters.includeArchived) {
    params.set("include_archived", "true");
  }
  return params;
}

export function fetchMunicipalAssetsPage(
  organizationId: number,
  filters: MunicipalAssetFilters,
  signal?: AbortSignal,
) {
  return adminRequestWithTotal<MunicipalAsset[]>(
    `/assets?${assetParams(organizationId, filters).toString()}`,
    "",
    "No se pudo cargar el inventario municipal.",
    { signal },
  );
}

async function fetchAssetCount(
  organizationId: number,
  filters: MunicipalAssetFilters,
  signal?: AbortSignal,
) {
  const response = await fetchMunicipalAssetsPage(
    organizationId,
    { ...filters, limit: 1, offset: 0 },
    signal,
  );
  return response.total;
}

export async function fetchAssetInventoryMetrics(
  organizationId: number,
  signal?: AbortSignal,
) {
  const [total, active, poor] = await Promise.all([
    fetchAssetCount(organizationId, {}, signal),
    fetchAssetCount(organizationId, { status: "active" }, signal),
    fetchAssetCount(
      organizationId,
      { conditionStatus: "poor" },
      signal,
    ),
  ]);
  return { total, active, poor };
}

export function createAssetCategory(
  accessToken: string,
  payload: AssetCategoryWrite,
) {
  return adminRequest<MunicipalAssetCategory>(
    "/assets/categories",
    accessToken,
    "No se pudo crear la categoría.",
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function updateAssetCategory(
  accessToken: string,
  categoryId: number,
  payload: Partial<Omit<AssetCategoryWrite, "organization_id">>,
) {
  return adminRequest<MunicipalAssetCategory>(
    `/assets/categories/${categoryId}`,
    accessToken,
    "No se pudo actualizar la categoría.",
    { method: "PATCH", body: JSON.stringify(payload) },
  );
}

export function createAssetType(
  accessToken: string,
  payload: AssetTypeWrite,
) {
  return adminRequest<MunicipalAssetType>(
    "/assets/types",
    accessToken,
    "No se pudo crear el tipo de activo.",
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function updateAssetType(
  accessToken: string,
  assetTypeId: number,
  payload: Partial<Omit<AssetTypeWrite, "organization_id">>,
) {
  return adminRequest<MunicipalAssetType>(
    `/assets/types/${assetTypeId}`,
    accessToken,
    "No se pudo actualizar el tipo de activo.",
    { method: "PATCH", body: JSON.stringify(payload) },
  );
}

export function createMunicipalAsset(
  accessToken: string,
  payload: MunicipalAssetWrite,
) {
  return adminRequest<MunicipalAsset>(
    "/assets",
    accessToken,
    "No se pudo crear el elemento del inventario.",
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function updateMunicipalAsset(
  accessToken: string,
  assetId: number,
  payload: MunicipalAssetPatch,
) {
  return adminRequest<MunicipalAsset>(
    `/assets/${assetId}`,
    accessToken,
    "No se pudo actualizar el elemento del inventario.",
    { method: "PATCH", body: JSON.stringify(payload) },
  );
}
