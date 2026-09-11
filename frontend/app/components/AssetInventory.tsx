"use client";

import {
  Archive,
  Boxes,
  CircleAlert,
  ClipboardList,
  MapPin,
  PackagePlus,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  Tags,
  X,
} from "lucide-react";
import Link from "next/link";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  createAssetCategory,
  createAssetType,
  createMunicipalAsset,
  fetchAssetCategories,
  fetchAssetInventoryMetrics,
  fetchAssetTypes,
  fetchMunicipalAssetsPage,
  updateAssetCategory,
  updateAssetType,
  updateMunicipalAsset,
  type MunicipalAssetPatch,
  type MunicipalAssetWrite,
} from "../lib/assets";
import { ApiRequestError } from "../lib/api";
import { useSession } from "../lib/session";
import type {
  AssetConditionStatus,
  AssetStatus,
  AssetTaxonomyStatus,
  MunicipalAsset,
  MunicipalAssetCategory,
  MunicipalAssetType,
  OrganizationSummary,
  User,
} from "./types";
import { userHasPermission } from "./types";
import styles from "./AssetInventory.module.css";
import { AssetDetailPanel } from "./AssetDetailPanel";

const PAGE_SIZE = 40;

const ASSET_STATUS_LABELS: Record<AssetStatus, string> = {
  active: "Activo",
  inactive: "Inactivo",
  retired: "Retirado",
  archived: "Archivado",
};

const CONDITION_LABELS: Record<AssetConditionStatus, string> = {
  good: "Bueno",
  fair: "Regular",
  poor: "Malo",
  unknown: "Sin revisar",
};

type InventoryFilters = {
  query: string;
  categoryId: string;
  assetTypeId: string;
  status: "" | AssetStatus;
  conditionStatus: "" | AssetConditionStatus;
  includeArchived: boolean;
};

type AssetDraft = {
  assetTypeId: string;
  code: string;
  name: string;
  description: string;
  status: AssetStatus;
  conditionStatus: AssetConditionStatus;
  material: string;
  dimensions: string;
  installedOn: string;
  lastInspectedOn: string;
  notes: string;
};

type AssetEditor = {
  asset: MunicipalAsset | null;
  draft: AssetDraft;
};

type CategoryDraft = {
  code: string;
  name: string;
  description: string;
  color: string;
  sortOrder: string;
  status: AssetTaxonomyStatus;
};

type TypeDraft = {
  categoryId: string;
  code: string;
  name: string;
  description: string;
  sortOrder: string;
  status: AssetTaxonomyStatus;
};

type TaxonomyEditor =
  | {
      kind: "category";
      category: MunicipalAssetCategory | null;
      draft: CategoryDraft;
    }
  | {
      kind: "type";
      assetType: MunicipalAssetType | null;
      draft: TypeDraft;
    };

type SharedTaxonomyDraft = Pick<
  CategoryDraft,
  "code" | "name" | "description" | "sortOrder" | "status"
>;

const EMPTY_FILTERS: InventoryFilters = {
  query: "",
  categoryId: "",
  assetTypeId: "",
  status: "",
  conditionStatus: "",
  includeArchived: false,
};

function isAbortError(error: unknown) {
  return error instanceof Error && error.name === "AbortError";
}

function optionalText(value: string) {
  const trimmed = value.trim();
  return trimmed || null;
}

function parsePositiveId(value: string) {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function parseSortOrder(value: string) {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

function formatDate(value: string | null) {
  if (!value) {
    return "—";
  }
  const date = new Date(value.includes("T") ? value : `${value}T12:00:00`);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("es-ES", {
    day: "numeric",
    month: "short",
    year: "numeric",
  }).format(date);
}

function organizationHasMunicipality(organization: OrganizationSummary) {
  return (
    typeof organization.municipality_id === "number" ||
    typeof organization.municipality?.id === "number"
  );
}

function emptyAssetDraft(assetTypeId = ""): AssetDraft {
  return {
    assetTypeId,
    code: "",
    name: "",
    description: "",
    status: "active",
    conditionStatus: "unknown",
    material: "",
    dimensions: "",
    installedOn: "",
    lastInspectedOn: "",
    notes: "",
  };
}

function assetDraftFrom(asset: MunicipalAsset): AssetDraft {
  return {
    assetTypeId: String(asset.asset_type_id),
    code: asset.code ?? "",
    name: asset.name,
    description: asset.description ?? "",
    status: asset.status,
    conditionStatus: asset.condition_status,
    material: asset.material ?? "",
    dimensions: asset.dimensions ?? "",
    installedOn: asset.installed_on ?? "",
    lastInspectedOn: asset.last_inspected_on ?? "",
    notes: asset.notes ?? "",
  };
}

function assetWriteFrom(
  organizationId: number,
  draft: AssetDraft,
): MunicipalAssetWrite | null {
  const assetTypeId = parsePositiveId(draft.assetTypeId);
  if (!assetTypeId || !draft.name.trim()) {
    return null;
  }
  return {
    organization_id: organizationId,
    asset_type_id: assetTypeId,
    code: optionalText(draft.code),
    name: draft.name.trim(),
    description: optionalText(draft.description),
    status: draft.status,
    condition_status: draft.conditionStatus,
    material: optionalText(draft.material),
    dimensions: optionalText(draft.dimensions),
    installed_on: optionalText(draft.installedOn),
    last_inspected_on: optionalText(draft.lastInspectedOn),
    notes: optionalText(draft.notes),
  };
}

function changedAssetFields(
  asset: MunicipalAsset,
  next: MunicipalAssetWrite,
): MunicipalAssetPatch {
  const current: Omit<MunicipalAssetWrite, "organization_id"> = {
    asset_type_id: asset.asset_type_id,
    code: asset.code,
    name: asset.name,
    description: asset.description,
    status: asset.status,
    condition_status: asset.condition_status,
    material: asset.material,
    dimensions: asset.dimensions,
    installed_on: asset.installed_on,
    last_inspected_on: asset.last_inspected_on,
    notes: asset.notes,
  };
  const candidate = { ...next };
  delete (candidate as Partial<MunicipalAssetWrite>).organization_id;
  const patch: MunicipalAssetPatch = {};

  for (const key of Object.keys(current) as Array<keyof typeof current>) {
    if (current[key] !== candidate[key]) {
      Object.assign(patch, { [key]: candidate[key] });
    }
  }
  return patch;
}

function categoryDraftFrom(
  category: MunicipalAssetCategory | null,
  sortOrder: number,
): CategoryDraft {
  return {
    code: category?.code ?? "",
    name: category?.name ?? "",
    description: category?.description ?? "",
    color: category?.color ?? "#3caf8c",
    sortOrder: String(category?.sort_order ?? sortOrder),
    status: category?.status ?? "active",
  };
}

function typeDraftFrom(
  assetType: MunicipalAssetType | null,
  categoryId: number | null,
  sortOrder: number,
): TypeDraft {
  return {
    categoryId: String(assetType?.category_id ?? categoryId ?? ""),
    code: assetType?.code ?? "",
    name: assetType?.name ?? "",
    description: assetType?.description ?? "",
    sortOrder: String(assetType?.sort_order ?? sortOrder),
    status: assetType?.status ?? "active",
  };
}

function inventoryErrorMessage(error: unknown, fallback: string) {
  if (!(error instanceof ApiRequestError)) {
    return null;
  }
  if (error.status === 401) {
    return null;
  }
  const messages: Record<string, string> = {
    "Asset category code already exists":
      "Ya existe una categoría con ese código.",
    "Asset type code already exists in category":
      "Ya existe un tipo con ese código dentro de la categoría.",
    "Asset code already exists":
      "Ya existe un elemento con ese código en la organización.",
    "Asset category must be active":
      "La categoría debe estar activa para utilizarla.",
    "Asset type must be active":
      "El tipo debe estar activo para utilizarlo.",
    "Asset has open maintenance orders":
      "No se puede retirar o archivar el elemento mientras tenga mantenimientos abiertos.",
  };
  return messages[error.message] ?? (error.message || fallback);
}

export function AssetInventory({
  user,
  initialOrganizationId,
  initialAssetId,
}: {
  user: User;
  initialOrganizationId?: number | null;
  initialAssetId?: number | null;
}) {
  const { getStoredToken, handleRequestError } = useSession();
  const organizations = useMemo(
    () =>
      (user.organizations ?? []).filter((organization) =>
        ["active", "paused"].includes(organization.status),
      ),
    [user.organizations],
  );
  const [organizationId, setOrganizationId] = useState(() =>
    organizations.some(({ id }) => id === initialOrganizationId)
      ? (initialOrganizationId ?? 0)
      : (organizations[0]?.id ?? 0),
  );
  const [categories, setCategories] = useState<MunicipalAssetCategory[]>([]);
  const [types, setTypes] = useState<MunicipalAssetType[]>([]);
  const [categoryTotal, setCategoryTotal] = useState(0);
  const [typeTotal, setTypeTotal] = useState(0);
  const [assets, setAssets] = useState<MunicipalAsset[]>([]);
  const [assetTotal, setAssetTotal] = useState(0);
  const [metrics, setMetrics] = useState({ total: 0, active: 0, poor: 0 });
  const [filters, setFilters] = useState<InventoryFilters>(EMPTY_FILTERS);
  const [queryInput, setQueryInput] = useState("");
  const [page, setPage] = useState(0);
  const [reloadVersion, setReloadVersion] = useState(0);
  const [isLoadingTaxonomy, setIsLoadingTaxonomy] = useState(false);
  const [isLoadingAssets, setIsLoadingAssets] = useState(false);
  const [taxonomyError, setTaxonomyError] = useState("");
  const [assetsError, setAssetsError] = useState("");
  const [message, setMessage] = useState("");
  const [assetEditor, setAssetEditor] = useState<AssetEditor | null>(null);
  const [assetFormError, setAssetFormError] = useState("");
  const [isSavingAsset, setIsSavingAsset] = useState(false);
  const [isTaxonomyOpen, setIsTaxonomyOpen] = useState(false);
  const [taxonomyEditor, setTaxonomyEditor] =
    useState<TaxonomyEditor | null>(null);
  const [taxonomyFormError, setTaxonomyFormError] = useState("");
  const [isSavingTaxonomy, setIsSavingTaxonomy] = useState(false);
  const [archiveTarget, setArchiveTarget] = useState<string | null>(null);
  const assetEditorRef = useRef<HTMLElement | null>(null);
  const taxonomyEditorRef = useRef<HTMLElement | null>(null);
  const handleRequestErrorRef = useRef(handleRequestError);

  const selectedOrganization = organizations.find(
    (organization) => organization.id === organizationId,
  );
  const hasViewPermission =
    userHasPermission(user, "assets.view") ||
    userHasPermission(user, "assets.manage");
  const hasCreatePermission =
    userHasPermission(user, "assets.create") ||
    userHasPermission(user, "assets.manage");
  const hasEditPermission =
    userHasPermission(user, "assets.edit") ||
    userHasPermission(user, "assets.manage");
  const hasArchivePermission =
    userHasPermission(user, "assets.archive") ||
    userHasPermission(user, "assets.manage");
  const canWriteOrganization = Boolean(
    selectedOrganization?.status === "active" &&
      organizationHasMunicipality(selectedOrganization),
  );
  const canCreate = hasCreatePermission && canWriteOrganization;
  const canEdit = hasEditPermission && canWriteOrganization;
  const canArchive = hasArchivePermission && canWriteOrganization;
  const canViewMap =
    userHasPermission(user, "map.view") || userHasPermission(user, "map.manage");

  const activeCategories = useMemo(
    () => categories.filter((category) => category.status === "active"),
    [categories],
  );
  const activeTypes = useMemo(
    () =>
      types.filter(
        (assetType) =>
          assetType.status === "active" &&
          assetType.category.status === "active",
      ),
    [types],
  );
  const filterTypes = useMemo(() => {
    const categoryId = parsePositiveId(filters.categoryId);
    return categoryId
      ? types.filter((assetType) => assetType.category_id === categoryId)
      : types;
  }, [filters.categoryId, types]);
  const pageCount = Math.max(1, Math.ceil(assetTotal / PAGE_SIZE));
  const hasActiveFilters =
    Boolean(filters.query) ||
    Boolean(filters.categoryId) ||
    Boolean(filters.assetTypeId) ||
    Boolean(filters.status) ||
    Boolean(filters.conditionStatus) ||
    filters.includeArchived;

  useEffect(() => {
    handleRequestErrorRef.current = handleRequestError;
  }, [handleRequestError]);

  useEffect(() => {
    if (!organizations.some((organization) => organization.id === organizationId)) {
      setOrganizationId(organizations[0]?.id ?? 0);
    }
  }, [organizationId, organizations]);

  useEffect(() => {
    setFilters(EMPTY_FILTERS);
    setQueryInput("");
    setPage(0);
    setCategories([]);
    setTypes([]);
    setCategoryTotal(0);
    setTypeTotal(0);
    setAssets([]);
    setAssetTotal(0);
    setMetrics({ total: 0, active: 0, poor: 0 });
    setTaxonomyError("");
    setAssetsError("");
    setAssetEditor(null);
    setIsTaxonomyOpen(false);
    setTaxonomyEditor(null);
    setMessage("");
  }, [organizationId]);

  useEffect(() => {
    if (!hasViewPermission || !organizationId) {
      setCategories([]);
      setTypes([]);
      setCategoryTotal(0);
      setTypeTotal(0);
      setMetrics({ total: 0, active: 0, poor: 0 });
      return;
    }
    const controller = new AbortController();
    setIsLoadingTaxonomy(true);
    setTaxonomyError("");

    void Promise.all([
      fetchAssetCategories(organizationId, controller.signal),
      fetchAssetTypes(organizationId, controller.signal),
      fetchAssetInventoryMetrics(organizationId, controller.signal),
    ])
      .then(([categoryResponse, typeResponse, metricResponse]) => {
        if (controller.signal.aborted) {
          return;
        }
        setCategories(categoryResponse.items);
        setCategoryTotal(categoryResponse.total);
        setTypes(typeResponse.items);
        setTypeTotal(typeResponse.total);
        setMetrics(metricResponse);
      })
      .catch((error) => {
        if (controller.signal.aborted || isAbortError(error)) {
          return;
        }
        const localMessage = inventoryErrorMessage(
          error,
          "No se pudo cargar la estructura del inventario.",
        );
        if (localMessage) {
          setTaxonomyError(localMessage);
        } else {
          handleRequestErrorRef.current(
            error,
            setTaxonomyError,
            "No se pudo cargar la estructura del inventario.",
          );
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setIsLoadingTaxonomy(false);
        }
      });

    return () => controller.abort();
  }, [hasViewPermission, organizationId, reloadVersion]);

  useEffect(() => {
    if (!hasViewPermission || !organizationId) {
      setAssets([]);
      setAssetTotal(0);
      return;
    }
    const controller = new AbortController();
    setIsLoadingAssets(true);
    setAssetsError("");

    void fetchMunicipalAssetsPage(
      organizationId,
      {
        query: filters.query,
        categoryId: parsePositiveId(filters.categoryId) ?? undefined,
        assetTypeId: parsePositiveId(filters.assetTypeId) ?? undefined,
        status: filters.status || undefined,
        conditionStatus: filters.conditionStatus || undefined,
        includeArchived: filters.includeArchived,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      },
      controller.signal,
    )
      .then((response) => {
        if (controller.signal.aborted) {
          return;
        }
        setAssetTotal(response.total);
        const lastAvailablePage = Math.max(
          0,
          Math.ceil(response.total / PAGE_SIZE) - 1,
        );
        if (page > lastAvailablePage) {
          setAssets([]);
          setPage(lastAvailablePage);
          return;
        }
        setAssets(response.items);
      })
      .catch((error) => {
        if (controller.signal.aborted || isAbortError(error)) {
          return;
        }
        const localMessage = inventoryErrorMessage(
          error,
          "No se pudo cargar el inventario municipal.",
        );
        if (localMessage) {
          setAssetsError(localMessage);
        } else {
          handleRequestErrorRef.current(
            error,
            setAssetsError,
            "No se pudo cargar el inventario municipal.",
          );
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setIsLoadingAssets(false);
        }
      });

    return () => controller.abort();
  }, [filters, hasViewPermission, organizationId, page, reloadVersion]);

  useEffect(() => {
    if (assetEditor) {
      assetEditorRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [assetEditor]);

  useEffect(() => {
    if (taxonomyEditor) {
      taxonomyEditorRef.current?.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    }
  }, [taxonomyEditor]);

  function reload(messageText?: string) {
    if (messageText) {
      setMessage(messageText);
    }
    setReloadVersion((version) => version + 1);
  }

  function applySearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPage(0);
    setFilters((current) => ({ ...current, query: queryInput.trim() }));
  }

  function clearFilters() {
    setQueryInput("");
    setFilters(EMPTY_FILTERS);
    setPage(0);
  }

  function updateTaxonomyDraft(patch: Partial<SharedTaxonomyDraft>) {
    setTaxonomyEditor((current) => {
      if (!current) {
        return current;
      }
      if (current.kind === "category") {
        return { ...current, draft: { ...current.draft, ...patch } };
      }
      return { ...current, draft: { ...current.draft, ...patch } };
    });
  }

  function openAssetCreate() {
    if (!canCreate) {
      return;
    }
    if (activeTypes.length === 0) {
      setIsTaxonomyOpen(true);
      if (activeCategories.length > 0) {
        openTypeCreate(activeCategories[0].id);
      } else {
        openCategoryCreate();
      }
      return;
    }
    setMessage("");
    setAssetFormError("");
    setAssetEditor({
      asset: null,
      draft: emptyAssetDraft(String(activeTypes[0].id)),
    });
  }

  function openAssetEdit(asset: MunicipalAsset) {
    if (!canEdit) {
      return;
    }
    setMessage("");
    setAssetFormError("");
    setAssetEditor({ asset, draft: assetDraftFrom(asset) });
  }

  async function saveAsset(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!assetEditor || !selectedOrganization) {
      return;
    }
    const payload = assetWriteFrom(selectedOrganization.id, assetEditor.draft);
    if (!payload) {
      setAssetFormError("Indica un nombre y un tipo de activo válido.");
      return;
    }
    if (
      payload.status === "archived" &&
      assetEditor.asset?.status !== "archived" &&
      !canArchive
    ) {
      setAssetFormError("No tienes permiso para archivar elementos.");
      return;
    }
    setIsSavingAsset(true);
    setAssetFormError("");
    try {
      if (assetEditor.asset) {
        const patch = changedAssetFields(assetEditor.asset, payload);
        if (Object.keys(patch).length === 0) {
          setAssetEditor(null);
          setMessage("No había cambios pendientes.");
          return;
        }
        await updateMunicipalAsset(
          getStoredToken(),
          assetEditor.asset.id,
          patch,
        );
        setAssetEditor(null);
        reload("Elemento actualizado correctamente.");
      } else {
        await createMunicipalAsset(getStoredToken(), payload);
        setAssetEditor(null);
        setPage(0);
        reload("Elemento añadido al inventario.");
      }
    } catch (error) {
      const localMessage = inventoryErrorMessage(
        error,
        "No se pudo guardar el elemento.",
      );
      if (localMessage) {
        setAssetFormError(localMessage);
      } else {
        handleRequestError(error, setAssetFormError, "No se pudo guardar el elemento.");
      }
    } finally {
      setIsSavingAsset(false);
    }
  }

  function nextCategorySortOrder() {
    return categories.reduce(
      (maximum, category) => Math.max(maximum, category.sort_order),
      -10,
    ) + 10;
  }

  function nextTypeSortOrder(categoryId: number | null) {
    return types
      .filter((assetType) => !categoryId || assetType.category_id === categoryId)
      .reduce(
        (maximum, assetType) => Math.max(maximum, assetType.sort_order),
        -10,
      ) + 10;
  }

  function openCategoryCreate() {
    if (!canCreate) {
      return;
    }
    setIsTaxonomyOpen(true);
    setTaxonomyFormError("");
    setTaxonomyEditor({
      kind: "category",
      category: null,
      draft: categoryDraftFrom(null, nextCategorySortOrder()),
    });
  }

  function openCategoryEdit(category: MunicipalAssetCategory) {
    if (!canEdit) {
      return;
    }
    setIsTaxonomyOpen(true);
    setTaxonomyFormError("");
    setTaxonomyEditor({
      kind: "category",
      category,
      draft: categoryDraftFrom(category, category.sort_order),
    });
  }

  function openTypeCreate(categoryId?: number) {
    if (!canCreate || activeCategories.length === 0) {
      return;
    }
    const selectedCategoryId =
      categoryId && activeCategories.some((item) => item.id === categoryId)
        ? categoryId
        : activeCategories[0].id;
    setIsTaxonomyOpen(true);
    setTaxonomyFormError("");
    setTaxonomyEditor({
      kind: "type",
      assetType: null,
      draft: typeDraftFrom(
        null,
        selectedCategoryId,
        nextTypeSortOrder(selectedCategoryId),
      ),
    });
  }

  function openTypeEdit(assetType: MunicipalAssetType) {
    if (!canEdit) {
      return;
    }
    setIsTaxonomyOpen(true);
    setTaxonomyFormError("");
    setTaxonomyEditor({
      kind: "type",
      assetType,
      draft: typeDraftFrom(assetType, assetType.category_id, assetType.sort_order),
    });
  }

  async function saveTaxonomy(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!taxonomyEditor || !selectedOrganization) {
      return;
    }
    setTaxonomyFormError("");
    setIsSavingTaxonomy(true);
    try {
      if (taxonomyEditor.kind === "category") {
        const { draft, category } = taxonomyEditor;
        const sortOrder = parseSortOrder(draft.sortOrder);
        if (!draft.code.trim() || !draft.name.trim() || sortOrder === null) {
          setTaxonomyFormError(
            "Completa el código, el nombre y un orden igual o superior a cero.",
          );
          return;
        }
        if (
          draft.status === "archived" &&
          category?.status !== "archived" &&
          !canArchive
        ) {
          setTaxonomyFormError("No tienes permiso para archivar categorías.");
          return;
        }
        const payload = {
          code: draft.code.trim().toLowerCase(),
          name: draft.name.trim(),
          description: optionalText(draft.description),
          color: optionalText(draft.color),
          sort_order: sortOrder,
          status: draft.status,
        };
        if (category) {
          const patch = Object.fromEntries(
            Object.entries(payload).filter(([key, value]) => {
              const current = {
                code: category.code,
                name: category.name,
                description: category.description,
                color: category.color,
                sort_order: category.sort_order,
                status: category.status,
              } as Record<string, unknown>;
              if (
                key === "color" &&
                category.color === null &&
                value === "#3caf8c"
              ) {
                return false;
              }
              return current[key] !== value;
            }),
          );
          if (Object.keys(patch).length > 0) {
            await updateAssetCategory(getStoredToken(), category.id, patch);
          }
          setTaxonomyEditor(null);
          reload(
            Object.keys(patch).length > 0
              ? "Categoría actualizada."
              : "No había cambios pendientes.",
          );
        } else {
          await createAssetCategory(getStoredToken(), {
            organization_id: selectedOrganization.id,
            ...payload,
          });
          setTaxonomyEditor(null);
          reload("Categoría creada. Ahora puedes añadir un tipo de activo.");
        }
      } else {
        const { draft, assetType } = taxonomyEditor;
        const categoryId = parsePositiveId(draft.categoryId);
        const sortOrder = parseSortOrder(draft.sortOrder);
        if (
          !categoryId ||
          !draft.code.trim() ||
          !draft.name.trim() ||
          sortOrder === null
        ) {
          setTaxonomyFormError(
            "Selecciona una categoría y completa el código, el nombre y el orden.",
          );
          return;
        }
        if (
          draft.status === "archived" &&
          assetType?.status !== "archived" &&
          !canArchive
        ) {
          setTaxonomyFormError("No tienes permiso para archivar tipos.");
          return;
        }
        const payload = {
          category_id: categoryId,
          code: draft.code.trim().toLowerCase(),
          name: draft.name.trim(),
          description: optionalText(draft.description),
          sort_order: sortOrder,
          status: draft.status,
        };
        if (assetType) {
          const current = {
            category_id: assetType.category_id,
            code: assetType.code,
            name: assetType.name,
            description: assetType.description,
            sort_order: assetType.sort_order,
            status: assetType.status,
          } as Record<string, unknown>;
          const patch = Object.fromEntries(
            Object.entries(payload).filter(([key, value]) => current[key] !== value),
          );
          if (Object.keys(patch).length > 0) {
            await updateAssetType(getStoredToken(), assetType.id, patch);
          }
          setTaxonomyEditor(null);
          reload(
            Object.keys(patch).length > 0
              ? "Tipo actualizado."
              : "No había cambios pendientes.",
          );
        } else {
          await createAssetType(getStoredToken(), {
            organization_id: selectedOrganization.id,
            ...payload,
          });
          setTaxonomyEditor(null);
          reload("Tipo creado. Ya puedes registrar elementos de este tipo.");
        }
      }
    } catch (error) {
      const localMessage = inventoryErrorMessage(
        error,
        "No se pudo guardar la taxonomía.",
      );
      if (localMessage) {
        setTaxonomyFormError(localMessage);
      } else {
        handleRequestError(
          error,
          setTaxonomyFormError,
          "No se pudo guardar la taxonomía.",
        );
      }
    } finally {
      setIsSavingTaxonomy(false);
    }
  }

  async function archiveRecord(
    kind: "category" | "type" | "asset",
    id: number,
    label: string,
  ) {
    if (!canArchive || archiveTarget) {
      return;
    }
    if (!window.confirm(`¿Archivar ${label}?`)) {
      return;
    }

    const targetKey = `${kind}:${id}`;
    const setTargetError = kind === "asset" ? setAssetsError : setTaxonomyError;
    setArchiveTarget(targetKey);
    setTargetError("");
    try {
      if (kind === "category") {
        await updateAssetCategory(getStoredToken(), id, { status: "archived" });
      } else if (kind === "type") {
        await updateAssetType(getStoredToken(), id, { status: "archived" });
      } else {
        await updateMunicipalAsset(getStoredToken(), id, { status: "archived" });
      }
      reload(`${label} se ha archivado.`);
    } catch (error) {
      const localMessage = inventoryErrorMessage(
        error,
        `No se pudo archivar ${label}.`,
      );
      if (localMessage) {
        setTargetError(localMessage);
      } else {
        handleRequestError(
          error,
          setTargetError,
          `No se pudo archivar ${label}.`,
        );
      }
    } finally {
      setArchiveTarget(null);
    }
  }

  if (!hasViewPermission) {
    return (
      <section className={styles.accessState}>
        <span className={styles.stateIcon}>
          <Archive aria-hidden="true" />
        </span>
        <p className={styles.eyebrow}>Inventario municipal</p>
        <h1>No tienes acceso al inventario</h1>
        <p>
          Tu cuenta necesita el permiso <code>assets.view</code> o
          <code> assets.manage</code> para consultar datos de esta sección.
        </p>
      </section>
    );
  }

  if (organizations.length === 0) {
    return (
      <section className={styles.accessState}>
        <span className={styles.stateIcon}>
          <Boxes aria-hidden="true" />
        </span>
        <p className={styles.eyebrow}>Inventario municipal</p>
        <h1>No hay una organización disponible</h1>
        <p>
          El inventario requiere una organización activa o pausada asociada a tu
          cuenta.
        </p>
      </section>
    );
  }

  return (
    <div className={styles.inventory}>
      <header className={styles.header}>
        <div>
          <p className={styles.eyebrow}>Patrimonio e instalaciones</p>
          <h1>Inventario municipal</h1>
          <p className={styles.lead}>
            Consulta, clasifica y mantiene los elementos reales del municipio.
          </p>
        </div>
        <div className={styles.headerActions}>
          {organizations.length > 1 ? (
            <label className={styles.organizationPicker}>
              <span>Organización</span>
              <select
                disabled={
                  isSavingAsset || isSavingTaxonomy || archiveTarget !== null
                }
                value={organizationId}
                onChange={(event) => setOrganizationId(Number(event.target.value))}
              >
                {organizations.map((organization) => (
                  <option key={organization.id} value={organization.id}>
                    {organization.name}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            <span className={styles.organizationName}>
              {selectedOrganization?.name}
            </span>
          )}
          <button
            className={styles.secondaryButton}
            disabled={isLoadingAssets || isLoadingTaxonomy}
            onClick={() => reload()}
            type="button"
          >
            <RefreshCw aria-hidden="true" />
            Actualizar
          </button>
          {canCreate ? (
            <button
              className={styles.primaryButton}
              onClick={openAssetCreate}
              type="button"
            >
              <Plus aria-hidden="true" />
              Nuevo elemento
            </button>
          ) : null}
        </div>
      </header>

      {selectedOrganization?.status === "paused" ? (
        <div className={styles.contextWarning} role="status">
          <CircleAlert aria-hidden="true" />
          <span>
            La organización está pausada. Puedes consultar el inventario, pero no
            modificarlo.
          </span>
        </div>
      ) : null}
      {selectedOrganization && !organizationHasMunicipality(selectedOrganization) ? (
        <div className={styles.contextWarning} role="status">
          <CircleAlert aria-hidden="true" />
          <span>
            Esta organización no tiene un municipio asociado; las altas y ediciones
            están deshabilitadas.
          </span>
        </div>
      ) : null}
      {message ? (
        <div className={styles.successMessage} role="status">
          {message}
          <button aria-label="Cerrar aviso" onClick={() => setMessage("")} type="button">
            <X aria-hidden="true" />
          </button>
        </div>
      ) : null}

      <dl className={styles.metrics} aria-label="Resumen del inventario">
        <div>
          <dt>Elementos</dt>
          <dd>{isLoadingTaxonomy ? "—" : metrics.total}</dd>
          <span>sin archivar</span>
        </div>
        <div>
          <dt>En servicio</dt>
          <dd>{isLoadingTaxonomy ? "—" : metrics.active}</dd>
          <span>estado activo</span>
        </div>
        <div>
          <dt>En mal estado</dt>
          <dd>{isLoadingTaxonomy ? "—" : metrics.poor}</dd>
          <span>requieren revisión</span>
        </div>
        <div>
          <dt>Clasificación</dt>
          <dd>{isLoadingTaxonomy ? "—" : `${categoryTotal} / ${typeTotal}`}</dd>
          <span>categorías / tipos</span>
        </div>
      </dl>

      {taxonomyError ? (
        <div className={styles.errorState} role="alert">
          <CircleAlert aria-hidden="true" />
          <div>
            <strong>No se pudo cargar la clasificación</strong>
            <p>{taxonomyError}</p>
          </div>
          <button onClick={() => reload()} type="button">
            Reintentar
          </button>
        </div>
      ) : null}

      <section className={styles.taxonomyPanel}>
        <button
          aria-expanded={isTaxonomyOpen}
          className={styles.taxonomyToggle}
          onClick={() => setIsTaxonomyOpen((open) => !open)}
          type="button"
        >
          <span>
            <Settings2 aria-hidden="true" />
            <span>
              <strong>Categorías y tipos</strong>
              <small>La estructura utilizada por los filtros y las altas</small>
            </span>
          </span>
          <span>{isTaxonomyOpen ? "Ocultar" : "Gestionar"}</span>
        </button>

        {isTaxonomyOpen ? (
          <div className={styles.taxonomyContent}>
            <div className={styles.taxonomyColumn}>
              <div className={styles.sectionHeading}>
                <div>
                  <Tags aria-hidden="true" />
                  <h2>Categorías</h2>
                </div>
                {canCreate ? (
                  <button onClick={openCategoryCreate} type="button">
                    <Plus aria-hidden="true" /> Añadir
                  </button>
                ) : null}
              </div>
              {categories.length > 0 ? (
                <ul className={styles.taxonomyList}>
                  {categories.map((category) => (
                    <li key={category.id}>
                      <span
                        className={styles.categoryDot}
                        style={{ backgroundColor: category.color ?? "var(--text-faint)" }}
                      />
                      <span>
                        <strong>{category.name}</strong>
                        <small>
                          {category.code}
                          {category.status === "archived" ? " · Archivada" : ""}
                        </small>
                      </span>
                      {canEdit || (canArchive && category.status !== "archived") ? (
                        <span className={styles.itemActions}>
                          {canEdit ? (
                            <button
                              aria-label={`Editar categoría ${category.name}`}
                              onClick={() => openCategoryEdit(category)}
                              type="button"
                            >
                              <Pencil aria-hidden="true" />
                            </button>
                          ) : null}
                          {canArchive && category.status !== "archived" ? (
                            <button
                              aria-label={`Archivar categoría ${category.name}`}
                              disabled={archiveTarget !== null}
                              onClick={() =>
                                void archiveRecord(
                                  "category",
                                  category.id,
                                  `la categoría ${category.name}`,
                                )
                              }
                              type="button"
                            >
                              <Archive aria-hidden="true" />
                            </button>
                          ) : null}
                        </span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className={styles.compactEmpty}>No hay categorías.</p>
              )}
            </div>
            <div className={styles.taxonomyColumn}>
              <div className={styles.sectionHeading}>
                <div>
                  <Boxes aria-hidden="true" />
                  <h2>Tipos</h2>
                </div>
                {canCreate && activeCategories.length > 0 ? (
                  <button onClick={() => openTypeCreate()} type="button">
                    <Plus aria-hidden="true" /> Añadir
                  </button>
                ) : null}
              </div>
              {types.length > 0 ? (
                <ul className={styles.taxonomyList}>
                  {types.map((assetType) => (
                    <li key={assetType.id}>
                      <span
                        className={styles.categoryDot}
                        style={{
                          backgroundColor:
                            assetType.category.color ?? "var(--text-faint)",
                        }}
                      />
                      <span>
                        <strong>{assetType.name}</strong>
                        <small>
                          {assetType.category.name} · {assetType.code}
                          {assetType.status === "archived" ? " · Archivado" : ""}
                        </small>
                      </span>
                      {canEdit || (canArchive && assetType.status !== "archived") ? (
                        <span className={styles.itemActions}>
                          {canEdit ? (
                            <button
                              aria-label={`Editar tipo ${assetType.name}`}
                              onClick={() => openTypeEdit(assetType)}
                              type="button"
                            >
                              <Pencil aria-hidden="true" />
                            </button>
                          ) : null}
                          {canArchive && assetType.status !== "archived" ? (
                            <button
                              aria-label={`Archivar tipo ${assetType.name}`}
                              disabled={archiveTarget !== null}
                              onClick={() =>
                                void archiveRecord(
                                  "type",
                                  assetType.id,
                                  `el tipo ${assetType.name}`,
                                )
                              }
                              type="button"
                            >
                              <Archive aria-hidden="true" />
                            </button>
                          ) : null}
                        </span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className={styles.compactEmpty}>No hay tipos de activo.</p>
              )}
            </div>
          </div>
        ) : null}
      </section>

      {taxonomyEditor ? (
        <section className={styles.editor} ref={taxonomyEditorRef}>
          <div className={styles.editorHeading}>
            <div>
              <p className={styles.eyebrow}>Clasificación</p>
              <h2>
                {taxonomyEditor.kind === "category"
                  ? taxonomyEditor.category
                    ? "Editar categoría"
                    : "Nueva categoría"
                  : taxonomyEditor.assetType
                    ? "Editar tipo"
                    : "Nuevo tipo"}
              </h2>
            </div>
            <button
              aria-label="Cerrar formulario"
              disabled={isSavingTaxonomy}
              onClick={() => setTaxonomyEditor(null)}
              type="button"
            >
              <X aria-hidden="true" />
            </button>
          </div>
          <form onSubmit={saveTaxonomy}>
            <div className={styles.formGrid}>
              {taxonomyEditor.kind === "type" ? (
                <label>
                  <span>Categoría</span>
                  <select
                    required
                    value={taxonomyEditor.draft.categoryId}
                    onChange={(event) =>
                      setTaxonomyEditor((current) =>
                        current?.kind === "type"
                          ? {
                              ...current,
                              draft: {
                                ...current.draft,
                                categoryId: event.target.value,
                              },
                            }
                          : current,
                      )
                    }
                  >
                    {taxonomyEditor.assetType &&
                    !activeCategories.some(
                      ({ id }) => id === taxonomyEditor.assetType?.category_id,
                    ) ? (
                      <option value={taxonomyEditor.assetType.category_id}>
                        {taxonomyEditor.assetType.category.name} (archivada)
                      </option>
                    ) : null}
                    {activeCategories.map((category) => (
                      <option key={category.id} value={category.id}>
                        {category.name}
                      </option>
                    ))}
                  </select>
                </label>
              ) : null}
              <label>
                <span>Código</span>
                <input
                  autoComplete="off"
                  maxLength={100}
                  pattern="[a-z0-9]+(?:[-_][a-z0-9]+)*"
                  placeholder="ej. alumbrado-publico"
                  required
                  value={taxonomyEditor.draft.code}
                  onChange={(event) =>
                    updateTaxonomyDraft({ code: event.target.value })
                  }
                />
                <small>Minúsculas, números, guiones o guion bajo.</small>
              </label>
              <label>
                <span>Nombre</span>
                <input
                  maxLength={255}
                  required
                  value={taxonomyEditor.draft.name}
                  onChange={(event) =>
                    updateTaxonomyDraft({ name: event.target.value })
                  }
                />
              </label>
              {taxonomyEditor.kind === "category" ? (
                <label>
                  <span>Color</span>
                  <input
                    className={styles.colorInput}
                    type="color"
                    value={taxonomyEditor.draft.color}
                    onChange={(event) =>
                      setTaxonomyEditor((current) =>
                        current?.kind === "category"
                          ? {
                              ...current,
                              draft: { ...current.draft, color: event.target.value },
                            }
                          : current,
                      )
                    }
                  />
                </label>
              ) : null}
              <label>
                <span>Orden</span>
                <input
                  min="0"
                  required
                  type="number"
                  value={taxonomyEditor.draft.sortOrder}
                  onChange={(event) =>
                    updateTaxonomyDraft({ sortOrder: event.target.value })
                  }
                />
              </label>
              <label>
                <span>Estado</span>
                <select
                  value={taxonomyEditor.draft.status}
                  onChange={(event) =>
                    updateTaxonomyDraft({
                      status: event.target.value as AssetTaxonomyStatus,
                    })
                  }
                >
                  <option value="active">Activa</option>
                  <option
                    disabled={
                      !canArchive &&
                      !(
                        taxonomyEditor.kind === "category"
                          ? taxonomyEditor.category?.status === "archived"
                          : taxonomyEditor.assetType?.status === "archived"
                      )
                    }
                    value="archived"
                  >
                    Archivada
                  </option>
                </select>
              </label>
              <label className={styles.fullField}>
                <span>Descripción</span>
                <textarea
                  rows={3}
                  value={taxonomyEditor.draft.description}
                  onChange={(event) =>
                    updateTaxonomyDraft({ description: event.target.value })
                  }
                />
              </label>
            </div>
            {taxonomyFormError ? (
              <p className={styles.formError} role="alert">
                {taxonomyFormError}
              </p>
            ) : null}
            <div className={styles.formActions}>
              <button
                className={styles.secondaryButton}
                disabled={isSavingTaxonomy}
                onClick={() => setTaxonomyEditor(null)}
                type="button"
              >
                Cancelar
              </button>
              <button
                className={styles.primaryButton}
                disabled={isSavingTaxonomy}
                type="submit"
              >
                {isSavingTaxonomy ? "Guardando…" : "Guardar"}
              </button>
            </div>
          </form>
        </section>
      ) : null}

      {initialAssetId && organizationId === initialOrganizationId ? (
        <AssetDetailPanel key={`${user.id}:${organizationId}:${initialAssetId}:${reloadVersion}`}
          assetId={initialAssetId} organizationId={organizationId} canViewMap={canViewMap}
          onEdit={canEdit ? openAssetEdit : undefined} />
      ) : null}

      {assetEditor ? (
        <section className={styles.editor} ref={assetEditorRef}>
          <div className={styles.editorHeading}>
            <div>
              <p className={styles.eyebrow}>Elemento municipal</p>
              <h2>{assetEditor.asset ? "Editar elemento" : "Nuevo elemento"}</h2>
            </div>
            <button
              aria-label="Cerrar formulario"
              disabled={isSavingAsset}
              onClick={() => setAssetEditor(null)}
              type="button"
            >
              <X aria-hidden="true" />
            </button>
          </div>
          <form onSubmit={saveAsset}>
            <div className={styles.formGrid}>
              <label>
                <span>Tipo</span>
                <select
                  required
                  value={assetEditor.draft.assetTypeId}
                  onChange={(event) =>
                    setAssetEditor((current) =>
                      current
                        ? {
                            ...current,
                            draft: {
                              ...current.draft,
                              assetTypeId: event.target.value,
                            },
                          }
                        : current,
                    )
                  }
                >
                  {assetEditor.asset &&
                  !activeTypes.some(
                    (assetType) => assetType.id === assetEditor.asset?.asset_type_id,
                  ) ? (
                    <option value={assetEditor.asset.asset_type_id}>
                      {assetEditor.asset.asset_type.category.name} ·{" "}
                      {assetEditor.asset.asset_type.name} (archivado)
                    </option>
                  ) : null}
                  {activeTypes.map((assetType) => (
                    <option key={assetType.id} value={assetType.id}>
                      {assetType.category.name} · {assetType.name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>Nombre</span>
                <input
                  maxLength={255}
                  required
                  value={assetEditor.draft.name}
                  onChange={(event) =>
                    setAssetEditor((current) =>
                      current
                        ? {
                            ...current,
                            draft: { ...current.draft, name: event.target.value },
                          }
                        : current,
                    )
                  }
                />
              </label>
              <label>
                <span>Código / identificador</span>
                <input
                  maxLength={100}
                  placeholder="Opcional"
                  value={assetEditor.draft.code}
                  onChange={(event) =>
                    setAssetEditor((current) =>
                      current
                        ? {
                            ...current,
                            draft: { ...current.draft, code: event.target.value },
                          }
                        : current,
                    )
                  }
                />
              </label>
              <label>
                <span>Estado</span>
                <select
                  value={assetEditor.draft.status}
                  onChange={(event) =>
                    setAssetEditor((current) =>
                      current
                        ? {
                            ...current,
                            draft: {
                              ...current.draft,
                              status: event.target.value as AssetStatus,
                            },
                          }
                        : current,
                    )
                  }
                >
                  <option value="active">Activo</option>
                  <option value="inactive">Inactivo</option>
                  <option value="retired">Retirado</option>
                  <option
                    disabled={
                      !canArchive && assetEditor.asset?.status !== "archived"
                    }
                    value="archived"
                  >
                    Archivado
                  </option>
                </select>
              </label>
              <label>
                <span>Conservación</span>
                <select
                  value={assetEditor.draft.conditionStatus}
                  onChange={(event) =>
                    setAssetEditor((current) =>
                      current
                        ? {
                            ...current,
                            draft: {
                              ...current.draft,
                              conditionStatus: event.target
                                .value as AssetConditionStatus,
                            },
                          }
                        : current,
                    )
                  }
                >
                  {Object.entries(CONDITION_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>Material</span>
                <input
                  maxLength={255}
                  value={assetEditor.draft.material}
                  onChange={(event) =>
                    setAssetEditor((current) =>
                      current
                        ? {
                            ...current,
                            draft: {
                              ...current.draft,
                              material: event.target.value,
                            },
                          }
                        : current,
                    )
                  }
                />
              </label>
              <label>
                <span>Dimensiones</span>
                <input
                  maxLength={500}
                  value={assetEditor.draft.dimensions}
                  onChange={(event) =>
                    setAssetEditor((current) =>
                      current
                        ? {
                            ...current,
                            draft: {
                              ...current.draft,
                              dimensions: event.target.value,
                            },
                          }
                        : current,
                    )
                  }
                />
              </label>
              <label>
                <span>Fecha de instalación</span>
                <input
                  type="date"
                  value={assetEditor.draft.installedOn}
                  onChange={(event) =>
                    setAssetEditor((current) =>
                      current
                        ? {
                            ...current,
                            draft: {
                              ...current.draft,
                              installedOn: event.target.value,
                            },
                          }
                        : current,
                    )
                  }
                />
              </label>
              <label>
                <span>Última inspección</span>
                <input
                  type="date"
                  value={assetEditor.draft.lastInspectedOn}
                  onChange={(event) =>
                    setAssetEditor((current) =>
                      current
                        ? {
                            ...current,
                            draft: {
                              ...current.draft,
                              lastInspectedOn: event.target.value,
                            },
                          }
                        : current,
                    )
                  }
                />
              </label>
              <label className={styles.fullField}>
                <span>Descripción</span>
                <textarea
                  rows={3}
                  value={assetEditor.draft.description}
                  onChange={(event) =>
                    setAssetEditor((current) =>
                      current
                        ? {
                            ...current,
                            draft: {
                              ...current.draft,
                              description: event.target.value,
                            },
                          }
                        : current,
                    )
                  }
                />
              </label>
              <label className={styles.fullField}>
                <span>Notas internas</span>
                <textarea
                  rows={3}
                  value={assetEditor.draft.notes}
                  onChange={(event) =>
                    setAssetEditor((current) =>
                      current
                        ? {
                            ...current,
                            draft: { ...current.draft, notes: event.target.value },
                          }
                        : current,
                    )
                  }
                />
              </label>
            </div>
            {assetFormError ? (
              <p className={styles.formError} role="alert">
                {assetFormError}
              </p>
            ) : null}
            <div className={styles.formActions}>
              <button
                className={styles.secondaryButton}
                disabled={isSavingAsset}
                onClick={() => setAssetEditor(null)}
                type="button"
              >
                Cancelar
              </button>
              <button
                className={styles.primaryButton}
                disabled={isSavingAsset}
                type="submit"
              >
                {isSavingAsset ? "Guardando…" : "Guardar elemento"}
              </button>
            </div>
          </form>
        </section>
      ) : null}

      <section className={styles.listPanel}>
        <div className={styles.listHeading}>
          <div>
            <p className={styles.eyebrow}>Elementos registrados</p>
            <h2>{isLoadingAssets ? "Cargando…" : `${assetTotal} resultados`}</h2>
          </div>
          {canCreate && activeTypes.length > 0 ? (
            <button className={styles.compactPrimary} onClick={openAssetCreate} type="button">
              <PackagePlus aria-hidden="true" /> Añadir elemento
            </button>
          ) : null}
        </div>

        <form className={styles.filters} onSubmit={applySearch}>
          <label className={styles.searchField}>
            <span className={styles.visuallyHidden}>Buscar en el inventario</span>
            <Search aria-hidden="true" />
            <input
              maxLength={200}
              placeholder="Buscar por nombre, código, material o descripción…"
              value={queryInput}
              onChange={(event) => setQueryInput(event.target.value)}
            />
            <button type="submit">Buscar</button>
          </label>
          <label>
            <span>Categoría</span>
            <select
              value={filters.categoryId}
              onChange={(event) => {
                setPage(0);
                setFilters((current) => ({
                  ...current,
                  categoryId: event.target.value,
                  assetTypeId: "",
                }));
              }}
            >
              <option value="">Todas</option>
              {categories.map((category) => (
                <option key={category.id} value={category.id}>
                  {category.name}
                  {category.status === "archived" ? " (archivada)" : ""}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Tipo</span>
            <select
              value={filters.assetTypeId}
              onChange={(event) => {
                setPage(0);
                setFilters((current) => ({
                  ...current,
                  assetTypeId: event.target.value,
                }));
              }}
            >
              <option value="">Todos</option>
              {filterTypes.map((assetType) => (
                <option key={assetType.id} value={assetType.id}>
                  {assetType.name}
                  {assetType.status === "archived" ? " (archivado)" : ""}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Estado</span>
            <select
              value={filters.status}
              onChange={(event) => {
                setPage(0);
                setFilters((current) => ({
                  ...current,
                  status: event.target.value as "" | AssetStatus,
                }));
              }}
            >
              <option value="">Todos</option>
              {Object.entries(ASSET_STATUS_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Conservación</span>
            <select
              value={filters.conditionStatus}
              onChange={(event) => {
                setPage(0);
                setFilters((current) => ({
                  ...current,
                  conditionStatus: event.target
                    .value as "" | AssetConditionStatus,
                }));
              }}
            >
              <option value="">Todas</option>
              {Object.entries(CONDITION_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label className={styles.archiveToggle}>
            <input
              checked={filters.includeArchived}
              onChange={(event) => {
                setPage(0);
                setFilters((current) => ({
                  ...current,
                  includeArchived: event.target.checked,
                }));
              }}
              type="checkbox"
            />
            Incluir archivados
          </label>
          {hasActiveFilters ? (
            <button className={styles.clearFilters} onClick={clearFilters} type="button">
              Limpiar filtros
            </button>
          ) : null}
        </form>

        {assetsError ? (
          <div className={styles.errorState} role="alert">
            <CircleAlert aria-hidden="true" />
            <div>
              <strong>No se pudieron cargar los elementos</strong>
              <p>{assetsError}</p>
            </div>
            <button onClick={() => reload()} type="button">
              Reintentar
            </button>
          </div>
        ) : isLoadingAssets ? (
          <div className={styles.loadingState} aria-live="polite">
            <span />
            <span />
            <span />
          </div>
        ) : assets.length > 0 ? (
          <>
            <div className={styles.tableWrap}>
              <table>
                <thead>
                  <tr>
                    <th>Elemento</th>
                    <th>Clasificación</th>
                    <th>Conservación</th>
                    <th>Estado</th>
                    <th>Ubicación</th>
                    <th>Actualizado</th>
                    {canEdit || canArchive ? (
                      <th><span className={styles.visuallyHidden}>Acciones</span></th>
                    ) : null}
                  </tr>
                </thead>
                <tbody>
                  {assets.map((asset) => (
                    <tr key={asset.id}>
                      <td data-label="Elemento">
                        <Link href={`/inventario?organization_id=${organizationId}&asset_id=${asset.id}`}><strong>{asset.name}</strong></Link>
                        <span>{asset.code || "Sin código"}</span>
                      </td>
                      <td data-label="Clasificación">
                        <span className={styles.categoryLabel}>
                          <i
                            style={{
                              backgroundColor:
                                asset.asset_type.category.color ??
                                "var(--text-faint)",
                            }}
                          />
                          {asset.asset_type.category.name}
                        </span>
                        <small>{asset.asset_type.name}</small>
                      </td>
                      <td data-label="Conservación">
                        <span
                          className={`${styles.condition} ${styles[`condition_${asset.condition_status}`]}`}
                        >
                          {CONDITION_LABELS[asset.condition_status]}
                        </span>
                      </td>
                      <td data-label="Estado">
                        <span className={styles.status}>
                          {ASSET_STATUS_LABELS[asset.status]}
                        </span>
                      </td>
                      <td data-label="Ubicación">
                        {canViewMap ? (
                          <Link
                            className={styles.mapLink}
                            href={`/mapa?organization_id=${organizationId}&entity_type=asset&entity_id=${asset.id}`}
                          >
                            <MapPin aria-hidden="true" />
                            {asset.location?.label || "Sin ubicar"}
                          </Link>
                        ) : (
                          <span>{asset.location?.label || "Sin ubicar"}</span>
                        )}
                      </td>
                      <td data-label="Actualizado">{formatDate(asset.updated_at)}</td>
                      {canEdit || canArchive ? (
                        <td className={styles.actionCell}>
                          <span className={styles.itemActions}>
                            {canEdit ? (
                              <button
                                aria-label={`Editar ${asset.name}`}
                                onClick={() => openAssetEdit(asset)}
                                type="button"
                              >
                                <Pencil aria-hidden="true" />
                              </button>
                            ) : null}
                            {canArchive && asset.status !== "archived" ? (
                              <button
                                aria-label={`Archivar ${asset.name}`}
                                disabled={archiveTarget !== null}
                                onClick={() =>
                                  void archiveRecord(
                                    "asset",
                                    asset.id,
                                    `el elemento ${asset.name}`,
                                  )
                                }
                                type="button"
                              >
                                <Archive aria-hidden="true" />
                              </button>
                            ) : null}
                          </span>
                        </td>
                      ) : null}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {pageCount > 1 ? (
              <nav className={styles.pagination} aria-label="Paginación del inventario">
                <button
                  disabled={page === 0}
                  onClick={() => setPage((current) => Math.max(0, current - 1))}
                  type="button"
                >
                  Anterior
                </button>
                <span>
                  Página {page + 1} de {pageCount}
                </span>
                <button
                  disabled={page + 1 >= pageCount}
                  onClick={() =>
                    setPage((current) => Math.min(pageCount - 1, current + 1))
                  }
                  type="button"
                >
                  Siguiente
                </button>
              </nav>
            ) : null}
          </>
        ) : (
          <div className={styles.emptyState}>
            <span className={styles.stateIcon}>
              {hasActiveFilters ? (
                <Search aria-hidden="true" />
              ) : activeTypes.length === 0 ? (
                <Tags aria-hidden="true" />
              ) : (
                <ClipboardList aria-hidden="true" />
              )}
            </span>
            {hasActiveFilters ? (
              <>
                <h3>No hay coincidencias</h3>
                <p>
                  Prueba con otros filtros o recupera la vista completa del
                  inventario.
                </p>
                <button className={styles.secondaryButton} onClick={clearFilters} type="button">
                  Limpiar filtros
                </button>
              </>
            ) : activeTypes.length === 0 ? (
              <>
                <h3>Prepara la estructura del inventario</h3>
                <p>
                  Antes de registrar elementos hace falta, como mínimo, una
                  categoría activa y un tipo de activo.
                </p>
                <ol className={styles.emptySteps}>
                  <li className={activeCategories.length > 0 ? styles.stepDone : ""}>
                    <span>1</span> Crear una categoría
                  </li>
                  <li><span>2</span> Definir un tipo</li>
                  <li><span>3</span> Añadir el primer elemento</li>
                </ol>
                {canCreate ? (
                  <button
                    className={styles.primaryButton}
                    onClick={
                      activeCategories.length > 0
                        ? () => openTypeCreate(activeCategories[0].id)
                        : openCategoryCreate
                    }
                    type="button"
                  >
                    <Plus aria-hidden="true" />
                    {activeCategories.length > 0
                      ? "Crear primer tipo"
                      : "Crear primera categoría"}
                  </button>
                ) : (
                  <p className={styles.permissionHint}>
                    Un responsable con permiso para crear inventario debe completar
                    esta configuración.
                  </p>
                )}
              </>
            ) : (
              <>
                <h3>Aún no hay elementos registrados</h3>
                <p>
                  La clasificación ya está preparada. Registra el primer bien,
                  instalación o equipo para empezar a construir el inventario real.
                </p>
                <div className={styles.emptyContext}>
                  <span>{activeCategories.length} categorías activas</span>
                  <span>{activeTypes.length} tipos disponibles</span>
                </div>
                {canCreate ? (
                  <button className={styles.primaryButton} onClick={openAssetCreate} type="button">
                    <PackagePlus aria-hidden="true" /> Añadir primer elemento
                  </button>
                ) : (
                  <p className={styles.permissionHint}>
                    Puedes consultar esta sección, pero tu cuenta no puede crear
                    elementos.
                  </p>
                )}
              </>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
