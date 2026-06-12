"use client";

import { type FormEvent, useRef, useState } from "react";
import type {
  Municipality,
  MunicipalityEditState,
} from "../../components/types";
import { adminRequest, adminRequestWithTotal } from "../api";

const ADMIN_PAGE_SIZE = 50;

export type MunicipalityListFilters = {
  q: string;
  province: string;
  autonomousCommunity: string;
  status: string;
  includeArchived: boolean;
  /** Página 1-indexada; el offset se calcula como (page - 1) * 50. */
  page: number;
};

const DEFAULT_MUNICIPALITY_FILTERS: MunicipalityListFilters = {
  q: "",
  province: "",
  autonomousCommunity: "",
  status: "",
  includeArchived: false,
  page: 1,
};

function buildMunicipalitiesQuery(filters: MunicipalityListFilters) {
  const params = new URLSearchParams();
  if (filters.q.trim()) {
    params.set("q", filters.q.trim());
  }
  if (filters.province) {
    params.set("province", filters.province);
  }
  if (filters.autonomousCommunity) {
    params.set("autonomous_community", filters.autonomousCommunity);
  }
  if (filters.status) {
    params.set("status", filters.status);
  }
  if (filters.includeArchived) {
    params.set("include_archived", "true");
  }
  params.set("limit", String(ADMIN_PAGE_SIZE));
  params.set("offset", String((filters.page - 1) * ADMIN_PAGE_SIZE));

  return `/municipalities?${params.toString()}`;
}

type RequestErrorHandler = (
  requestError: unknown,
  setMessage: (message: string) => void,
  fallback: string,
) => void;

type UseMunicipalitiesAdminArgs = {
  getStoredToken: () => string;
  handleRequestError: RequestErrorHandler;
  /**
   * view||manage: cuando es false (usuario solo-crear/editar/archivar) no se
   * recarga la lista tras las mutaciones, porque el GET daría un 403 seguro.
   */
  canList?: boolean;
};

const EMPTY_MUNICIPALITY_FORM: MunicipalityEditState = {
  name: "",
  province: "",
  autonomous_community: "",
  country: "España",
  ine_code: "",
  population: "",
  surface_km2: "",
  density: "",
  postal_codes: "",
  municipality_type: "municipality",
  rural_urban_profile: "unknown",
  economic_profile: "",
  tourism_profile: "",
  geographic_notes: "",
  administrative_notes: "",
  status: "active",
};

function createEmptyMunicipalityForm(): MunicipalityEditState {
  return { ...EMPTY_MUNICIPALITY_FORM };
}

function buildMunicipalityEditState(municipalities: Municipality[]) {
  return municipalities.reduce<Record<number, MunicipalityEditState>>(
    (edits, municipality) => {
      edits[municipality.id] = {
        name: municipality.name,
        province: municipality.province,
        autonomous_community: municipality.autonomous_community,
        country: municipality.country,
        ine_code: municipality.ine_code ?? "",
        population:
          municipality.population === null ? "" : String(municipality.population),
        surface_km2:
          municipality.surface_km2 === null
            ? ""
            : String(municipality.surface_km2),
        density: municipality.density === null ? "" : String(municipality.density),
        postal_codes: municipality.postal_codes ?? "",
        municipality_type: municipality.municipality_type,
        rural_urban_profile: municipality.rural_urban_profile,
        economic_profile: municipality.economic_profile ?? "",
        tourism_profile: municipality.tourism_profile ?? "",
        geographic_notes: municipality.geographic_notes ?? "",
        administrative_notes: municipality.administrative_notes ?? "",
        status: municipality.status,
      };
      return edits;
    },
    {},
  );
}

function parseOptionalNumber(value: string) {
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }

  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : Number.NaN;
}

function buildMunicipalityPayload(edit: MunicipalityEditState) {
  const population = parseOptionalNumber(edit.population);
  if (
    population !== null &&
    (!Number.isInteger(population) || population < 0)
  ) {
    throw new Error("La población debe ser un número entero no negativo.");
  }

  const surfaceKm2 = parseOptionalNumber(edit.surface_km2);
  if (surfaceKm2 !== null && surfaceKm2 < 0) {
    throw new Error("La superficie debe ser un número no negativo.");
  }

  const density = parseOptionalNumber(edit.density);
  if (density !== null && density < 0) {
    throw new Error("La densidad debe ser un número no negativo.");
  }

  return {
    name: edit.name,
    province: edit.province,
    autonomous_community: edit.autonomous_community,
    country: edit.country,
    ine_code: edit.ine_code.trim() || null,
    population,
    surface_km2: surfaceKm2,
    density,
    postal_codes: edit.postal_codes.trim() || null,
    municipality_type: edit.municipality_type,
    rural_urban_profile: edit.rural_urban_profile,
    economic_profile: edit.economic_profile.trim() || null,
    tourism_profile: edit.tourism_profile.trim() || null,
    geographic_notes: edit.geographic_notes.trim() || null,
    administrative_notes: edit.administrative_notes.trim() || null,
    status: edit.status,
  };
}

export function useMunicipalitiesAdmin({
  getStoredToken,
  handleRequestError,
  canList = true,
}: UseMunicipalitiesAdminArgs) {
  const [municipalities, setMunicipalities] = useState<Municipality[]>([]);
  const [isLoadingMunicipalities, setIsLoadingMunicipalities] =
    useState(false);
  const [municipalitiesError, setMunicipalitiesError] = useState("");

  const [municipalityTotal, setMunicipalityTotal] = useState(0);
  // Últimos filtros aplicados; los reload internos tras crear/editar/archivar
  // reutilizan exactamente la misma consulta que pidió la página (URL).
  const lastFiltersRef = useRef<MunicipalityListFilters>(
    DEFAULT_MUNICIPALITY_FILTERS,
  );
  const [newMunicipality, setNewMunicipality] =
    useState<MunicipalityEditState>(createEmptyMunicipalityForm);
  const [municipalityFormError, setMunicipalityFormError] = useState("");
  const [isCreatingMunicipality, setIsCreatingMunicipality] = useState(false);
  const [municipalityEdits, setMunicipalityEdits] = useState<
    Record<number, MunicipalityEditState>
  >({});
  const [municipalityEditError, setMunicipalityEditError] = useState("");
  const [municipalityEditMessage, setMunicipalityEditMessage] = useState("");
  const [updatingMunicipalityId, setUpdatingMunicipalityId] = useState<
    number | null
  >(null);

  async function loadMunicipalities(
    filters: MunicipalityListFilters = lastFiltersRef.current,
  ) {
    lastFiltersRef.current = filters;
    setIsLoadingMunicipalities(true);
    setMunicipalitiesError("");

    try {
      const token = getStoredToken();
      const { items, total } = await adminRequestWithTotal<Municipality[]>(
        buildMunicipalitiesQuery(filters),
        token,
        "No se pudo cargar la lista de municipios.",
      );

      setMunicipalities(items);
      setMunicipalityTotal(total);
      setMunicipalityEdits(buildMunicipalityEditState(items));
    } catch (loadError) {
      handleRequestError(
        loadError,
        setMunicipalitiesError,
        "No se pudo cargar la lista de municipios.",
      );
    } finally {
      setIsLoadingMunicipalities(false);
    }
  }

  function updateNewMunicipality(updates: Partial<MunicipalityEditState>) {
    setNewMunicipality((currentMunicipality) => ({
      ...currentMunicipality,
      ...updates,
    }));
  }

  function updateMunicipalityEdit(
    municipalityId: number,
    updates: Partial<MunicipalityEditState>,
  ) {
    setMunicipalityEdits((currentEdits) => {
      const currentEdit = currentEdits[municipalityId];
      if (!currentEdit) {
        return currentEdits;
      }

      return {
        ...currentEdits,
        [municipalityId]: {
          ...currentEdit,
          ...updates,
        },
      };
    });
  }

  async function handleCreateMunicipality(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMunicipalityFormError("");
    setIsCreatingMunicipality(true);

    try {
      const token = getStoredToken();
      await adminRequest<Municipality>(
        "/municipalities",
        token,
        "No se pudo crear el municipio.",
        {
          method: "POST",
          body: JSON.stringify(buildMunicipalityPayload(newMunicipality)),
        },
      );

      setNewMunicipality(createEmptyMunicipalityForm());
      if (canList) {
        await loadMunicipalities();
      }
    } catch (createError) {
      handleRequestError(
        createError,
        setMunicipalityFormError,
        "No se pudo crear el municipio.",
      );
    } finally {
      setIsCreatingMunicipality(false);
    }
  }

  async function handleUpdateMunicipality(municipalityId: number) {
    const edit = municipalityEdits[municipalityId];
    if (!edit) {
      setMunicipalityEditError("No se pudo encontrar el municipio para editar.");
      return;
    }

    setMunicipalityEditError("");
    setMunicipalityEditMessage("");
    setUpdatingMunicipalityId(municipalityId);

    try {
      const token = getStoredToken();
      await adminRequest<Municipality>(
        `/municipalities/${municipalityId}`,
        token,
        "No se pudo actualizar el municipio.",
        {
          method: "PATCH",
          body: JSON.stringify(buildMunicipalityPayload(edit)),
        },
      );

      setMunicipalityEditMessage("Municipio actualizado.");
      if (canList) {
        await loadMunicipalities();
      }
    } catch (updateError) {
      handleRequestError(
        updateError,
        setMunicipalityEditError,
        "No se pudo actualizar el municipio.",
      );
    } finally {
      setUpdatingMunicipalityId(null);
    }
  }

  async function handleArchiveMunicipality(municipality: Municipality) {
    setMunicipalityEditError("");
    setMunicipalityEditMessage("");

    if (municipality.status === "archived") {
      setMunicipalityEditError("El municipio ya está archivado.");
      return;
    }

    const confirmed = window.confirm(
      `¿Archivar el municipio ${municipality.name}? No se eliminará definitivamente.`,
    );
    if (!confirmed) {
      return;
    }

    setUpdatingMunicipalityId(municipality.id);

    try {
      const token = getStoredToken();
      await adminRequest<Municipality>(
        `/municipalities/${municipality.id}`,
        token,
        "No se pudo archivar el municipio.",
        {
          method: "PATCH",
          body: JSON.stringify({ status: "archived" }),
        },
      );

      setMunicipalityEditMessage("Municipio archivado.");
      if (canList) {
        await loadMunicipalities();
      }
    } catch (archiveError) {
      handleRequestError(
        archiveError,
        setMunicipalityEditError,
        "No se pudo archivar el municipio.",
      );
    } finally {
      setUpdatingMunicipalityId(null);
    }
  }

  return {
    municipalities,
    isLoadingMunicipalities,
    municipalitiesError,
    adminPageSize: ADMIN_PAGE_SIZE,
    municipalityTotal,
    newMunicipality,
    municipalityFormError,
    isCreatingMunicipality,
    municipalityEdits,
    municipalityEditError,
    municipalityEditMessage,
    updatingMunicipalityId,
    loadMunicipalities,
    updateNewMunicipality,
    updateMunicipalityEdit,
    handleCreateMunicipality,
    handleUpdateMunicipality,
    handleArchiveMunicipality,
  };
}
