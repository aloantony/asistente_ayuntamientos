"use client";

import {
  BookOpen,
  ChevronLeft,
  ChevronRight,
  Filter,
  Search,
  ShieldCheck,
  X,
} from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  ORDINANCE_LIBRARY_PAGE_SIZE,
  fetchOrdinanceComparison,
  fetchOrdinanceDetail,
  fetchOrdinanceSearch,
  type OrdinanceSearchFilters,
} from "../lib/ordinances";
import {
  canCompareOrdinances,
  canViewOrdinanceLibrary,
} from "../lib/permissions";
import { useSession } from "../lib/session";
import type {
  Ordinance,
  OrdinanceComparison,
  OrdinanceLegalChunk,
  OrdinanceResultScope,
  OrdinanceSearchPage,
  OrdinanceSemanticSearchResult,
} from "./types";
import { OrdinanceComparisonPanel } from "./OrdinanceComparisonPanel";
import { OrdinanceDetailPanel } from "./OrdinanceDetailPanel";
import { OrdinanceSearchResults } from "./OrdinanceSearchResults";
import styles from "./OrdinanceLibrary.module.css";

type FilterDraft = {
  query: string;
  municipalityName: string;
  province: string;
  topic: string;
  strictTopic: boolean;
  populationGte: string;
  populationLt: string;
  resultScope: OrdinanceResultScope;
  includeInactive: boolean;
};

const RESULT_SCOPES: Array<{
  value: OrdinanceResultScope;
  label: string;
  description: string;
}> = [
  {
    value: "ordinances",
    label: "Una por ordenanza",
    description: "La mejor coincidencia de cada norma.",
  },
  {
    value: "municipalities",
    label: "Una por municipio",
    description: "Explora diversidad territorial.",
  },
  {
    value: "fragments",
    label: "Todos los fragmentos",
    description: "Vista jurídica más exhaustiva.",
  },
];

const FILTER_PARAM_KEYS = [
  "q",
  "municipality_id",
  "municipality_name",
  "province",
  "topic",
  "strict_topic",
  "population_gte",
  "population_lt",
  "result_scope",
  "include_inactive",
  "page",
] as const;

function isResultScope(value: string | null): value is OrdinanceResultScope {
  return RESULT_SCOPES.some((scope) => scope.value === value);
}

function parsePositiveInteger(value: string | null) {
  if (!value) {
    return null;
  }
  const parsed = Number.parseInt(value, 10);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

function parseNonNegativeInteger(value: string | null) {
  if (value === null || value === "") {
    return undefined;
  }
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : undefined;
}

function parseComparisonIds(value: string | null) {
  if (!value) {
    return [];
  }
  return Array.from(
    new Set(
      value
        .split(",")
        .map((item) => parsePositiveInteger(item.trim()))
        .filter((item): item is number => item !== null),
    ),
  ).slice(0, 4);
}

function readFilterDraft(params: URLSearchParams): FilterDraft {
  const resultScope = params.get("result_scope");
  return {
    query: params.get("q") ?? "",
    municipalityName: params.get("municipality_name") ?? "",
    province: params.get("province") ?? "",
    topic: params.get("topic") ?? "",
    strictTopic: params.get("strict_topic") === "true",
    populationGte: params.get("population_gte") ?? "",
    populationLt: params.get("population_lt") ?? "",
    resultScope: isResultScope(resultScope) ? resultScope : "ordinances",
    includeInactive: params.get("include_inactive") === "true",
  };
}

function buildFilterSignature(paramsSignature: string) {
  const current = new URLSearchParams(paramsSignature);
  const filters = new URLSearchParams();
  FILTER_PARAM_KEYS.forEach((key) => {
    const value = current.get(key);
    if (value !== null) {
      filters.set(key, value);
    }
  });
  return filters.toString();
}

function hasAdvancedFilterParams(paramsSignature: string) {
  const params = new URLSearchParams(paramsSignature);
  return [
    "municipality_id",
    "municipality_name",
    "province",
    "topic",
    "strict_topic",
    "population_gte",
    "population_lt",
    "result_scope",
    "include_inactive",
  ].some((key) => params.has(key));
}

function isAbortError(error: unknown) {
  return error instanceof Error && error.name === "AbortError";
}

export function OrdinanceLibrary() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const paramsSignature = searchParams.toString();
  const filterSignature = useMemo(
    () => buildFilterSignature(paramsSignature),
    [paramsSignature],
  );
  const { user, handleRequestError } = useSession();
  const handleRequestErrorRef = useRef(handleRequestError);
  const detailTriggerRef = useRef<HTMLElement | null>(null);
  const [draft, setDraft] = useState<FilterDraft>(() =>
    readFilterDraft(new URLSearchParams(filterSignature)),
  );
  const [searchPage, setSearchPage] = useState<OrdinanceSearchPage | null>(null);
  const [searchPageSignature, setSearchPageSignature] = useState<string | null>(
    null,
  );
  const [isSearching, setIsSearching] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [validationError, setValidationError] = useState("");
  const [detail, setDetail] = useState<{
    ordinance: Ordinance;
    chunks: OrdinanceLegalChunk[];
  } | null>(null);
  const [isLoadingDetail, setIsLoadingDetail] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [comparison, setComparison] = useState<OrdinanceComparison | null>(null);
  const [isLoadingComparison, setIsLoadingComparison] = useState(false);
  const [comparisonError, setComparisonError] = useState("");
  const [comparisonNotice, setComparisonNotice] = useState("");
  const [advancedFiltersOpen, setAdvancedFiltersOpen] = useState(() =>
    hasAdvancedFilterParams(filterSignature),
  );

  useEffect(() => {
    handleRequestErrorRef.current = handleRequestError;
  }, [handleRequestError]);

  useEffect(() => {
    setDraft(readFilterDraft(new URLSearchParams(filterSignature)));
    setValidationError("");
    setAdvancedFiltersOpen(hasAdvancedFilterParams(filterSignature));
  }, [filterSignature]);

  const appliedFilters = useMemo(() => {
    const params = new URLSearchParams(filterSignature);
    const page = parsePositiveInteger(params.get("page")) ?? 1;
    const scope = params.get("result_scope");
    return {
      query: params.get("q")?.trim() ?? "",
      municipalityId: parsePositiveInteger(params.get("municipality_id")) ??
        undefined,
      municipalityName: params.get("municipality_name") ?? undefined,
      province: params.get("province") ?? undefined,
      topic: params.get("topic") ?? undefined,
      strictTopic: params.get("strict_topic") === "true",
      populationGte: parseNonNegativeInteger(params.get("population_gte")),
      populationLt: parseNonNegativeInteger(params.get("population_lt")),
      resultScope: isResultScope(scope) ? scope : "ordinances",
      includeInactive: params.get("include_inactive") === "true",
      page,
    } satisfies OrdinanceSearchFilters;
  }, [filterSignature]);

  const applied = useMemo(() => {
    const params = new URLSearchParams(paramsSignature);
    return {
      filters: appliedFilters,
      municipalityId: parsePositiveInteger(params.get("municipality_id")),
      selectedOrdinanceId: parsePositiveInteger(params.get("id")),
      comparisonMunicipalityIds: parseComparisonIds(params.get("compare")),
    };
  }, [appliedFilters, paramsSignature]);

  const canView = Boolean(user && canViewOrdinanceLibrary(user));
  const canCompare = Boolean(user && canCompareOrdinances(user));
  const comparisonSignature = applied.comparisonMunicipalityIds.join(",");

  useEffect(() => {
    if (!user || !canView || applied.filters.query.length < 2) {
      setSearchPage(null);
      setSearchPageSignature(null);
      setSearchError("");
      setIsSearching(false);
      return;
    }

    const controller = new AbortController();
    setSearchPage(null);
    setSearchPageSignature(null);
    setIsSearching(true);
    setSearchError("");

    fetchOrdinanceSearch(applied.filters, controller.signal)
      .then((page) => {
        if (!controller.signal.aborted) {
          setSearchPage(page);
          setSearchPageSignature(filterSignature);
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted && !isAbortError(error)) {
          handleRequestErrorRef.current(
            error,
            setSearchError,
            "No se pudo consultar el repositorio de ordenanzas.",
          );
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setIsSearching(false);
        }
      });

    return () => controller.abort();
  }, [applied.filters, canView, filterSignature, user]);

  useEffect(() => {
    if (
      !searchPage ||
      searchPageSignature !== filterSignature ||
      isSearching ||
      searchPage.total_matches < 1
    ) {
      return;
    }
    const currentPage = applied.filters.page ?? 1;
    const lastPage = Math.max(
      1,
      Math.ceil(searchPage.total_matches / ORDINANCE_LIBRARY_PAGE_SIZE),
    );
    if (currentPage <= lastPage || searchPage.results.length > 0) {
      return;
    }
    const next = new URLSearchParams(paramsSignature);
    if (lastPage === 1) {
      next.delete("page");
    } else {
      next.set("page", String(lastPage));
    }
    next.delete("id");
    const query = next.toString();
    router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
  }, [
    applied.filters.page,
    isSearching,
    paramsSignature,
    pathname,
    router,
    filterSignature,
    searchPage,
    searchPageSignature,
  ]);

  useEffect(() => {
    const ordinanceId = applied.selectedOrdinanceId;
    if (!user || !canView || !ordinanceId) {
      setDetail(null);
      setDetailError("");
      setIsLoadingDetail(false);
      return;
    }

    const controller = new AbortController();
    setDetail(null);
    setDetailError("");
    setIsLoadingDetail(true);

    fetchOrdinanceDetail(ordinanceId, {
      includeInactive: applied.filters.includeInactive,
      signal: controller.signal,
    })
      .then((loadedDetail) => {
        if (!controller.signal.aborted) {
          setDetail(loadedDetail);
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted && !isAbortError(error)) {
          handleRequestErrorRef.current(
            error,
            setDetailError,
            "No se pudo cargar la ficha de la ordenanza.",
          );
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setIsLoadingDetail(false);
        }
      });

    return () => controller.abort();
  }, [
    applied.filters.includeInactive,
    applied.selectedOrdinanceId,
    canView,
    user,
  ]);

  useEffect(() => {
    const municipalityIds = parseComparisonIds(comparisonSignature);
    if (!user || !canCompare || municipalityIds.length < 2) {
      setComparison(null);
      setComparisonError("");
      setIsLoadingComparison(false);
      return;
    }

    const controller = new AbortController();
    setComparison(null);
    setComparisonError("");
    setIsLoadingComparison(true);

    fetchOrdinanceComparison(municipalityIds, {
      topic: applied.filters.strictTopic ? applied.filters.topic : undefined,
      includeInactive: applied.filters.includeInactive,
      signal: controller.signal,
    })
      .then((loadedComparison) => {
        if (!controller.signal.aborted) {
          setComparison(loadedComparison);
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted && !isAbortError(error)) {
          handleRequestErrorRef.current(
            error,
            setComparisonError,
            "No se pudo preparar la comparación municipal.",
          );
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setIsLoadingComparison(false);
        }
      });

    return () => controller.abort();
  }, [
    applied.filters.includeInactive,
    applied.filters.strictTopic,
    applied.filters.topic,
    canCompare,
    comparisonSignature,
    user,
  ]);

  function navigate(params: URLSearchParams, replace = false) {
    const query = params.toString();
    const href = query ? `${pathname}?${query}` : pathname;
    if (replace) {
      router.replace(href, { scroll: false });
    } else {
      router.push(href, { scroll: false });
    }
  }

  function handleSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const query = draft.query.trim();
    if (query.length < 2) {
      setValidationError("Escribe al menos dos caracteres para buscar.");
      return;
    }

    const populationGte = parseNonNegativeInteger(draft.populationGte);
    const populationLt = parseNonNegativeInteger(draft.populationLt);
    if (
      (draft.populationGte && populationGte === undefined) ||
      (draft.populationLt && populationLt === undefined)
    ) {
      setValidationError("Los límites de población deben ser enteros positivos.");
      return;
    }
    if (
      populationGte !== undefined &&
      populationLt !== undefined &&
      populationGte >= populationLt
    ) {
      setValidationError(
        "La población mínima debe ser menor que el límite superior.",
      );
      return;
    }

    const current = new URLSearchParams(paramsSignature);
    const next = new URLSearchParams();
    next.set("q", query);
    if (applied.municipalityId) {
      next.set("municipality_id", String(applied.municipalityId));
    }
    if (draft.municipalityName.trim()) {
      next.set("municipality_name", draft.municipalityName.trim());
    }
    if (draft.province.trim()) {
      next.set("province", draft.province.trim());
    }
    if (draft.topic.trim()) {
      next.set("topic", draft.topic.trim());
      if (draft.strictTopic) {
        next.set("strict_topic", "true");
      }
    }
    if (populationGte !== undefined) {
      next.set("population_gte", String(populationGte));
    }
    if (populationLt !== undefined) {
      next.set("population_lt", String(populationLt));
    }
    if (draft.resultScope !== "ordinances") {
      next.set("result_scope", draft.resultScope);
    }
    if (draft.includeInactive) {
      next.set("include_inactive", "true");
    }
    const compare = current.get("compare");
    if (compare) {
      next.set("compare", compare);
    }
    setValidationError("");
    navigate(next);
  }

  function runSuggestedQuery(query: string) {
    const next = new URLSearchParams();
    next.set("q", query);
    if (applied.municipalityId) {
      next.set("municipality_id", String(applied.municipalityId));
    }
    navigate(next);
  }

  function clearFilters() {
    navigate(new URLSearchParams());
  }

  function removeMunicipalityContext() {
    const next = new URLSearchParams(paramsSignature);
    next.delete("municipality_id");
    next.delete("page");
    navigate(next);
  }

  function selectOrdinance(
    ordinanceId: number,
    trigger?: HTMLElement,
  ) {
    if (trigger) {
      detailTriggerRef.current = trigger;
    }
    const next = new URLSearchParams(paramsSignature);
    next.set("id", String(ordinanceId));
    navigate(next);
  }

  function closeDetail() {
    const trigger = detailTriggerRef.current;
    const next = new URLSearchParams(paramsSignature);
    next.delete("id");
    navigate(next, true);
    window.requestAnimationFrame(() => trigger?.focus());
  }

  function toggleComparison(result: OrdinanceSemanticSearchResult) {
    const currentIds = applied.comparisonMunicipalityIds;
    const selected = currentIds.includes(result.municipality_id);
    if (!selected && currentIds.length >= 4) {
      setComparisonNotice("Puedes comparar un máximo de cuatro municipios.");
      return;
    }

    const nextIds = selected
      ? currentIds.filter((id) => id !== result.municipality_id)
      : [...currentIds, result.municipality_id];
    const next = new URLSearchParams(paramsSignature);
    if (nextIds.length > 0) {
      next.set("compare", nextIds.join(","));
    } else {
      next.delete("compare");
    }
    setComparisonNotice("");
    navigate(next, true);
  }

  function removeComparisonMunicipality(municipalityId: number) {
    const nextIds = applied.comparisonMunicipalityIds.filter(
      (id) => id !== municipalityId,
    );
    const next = new URLSearchParams(paramsSignature);
    if (nextIds.length > 0) {
      next.set("compare", nextIds.join(","));
    } else {
      next.delete("compare");
    }
    setComparisonNotice("");
    navigate(next, true);
  }

  function closeComparison() {
    const next = new URLSearchParams(paramsSignature);
    next.delete("compare");
    navigate(next, true);
  }

  function goToPage(page: number) {
    const next = new URLSearchParams(paramsSignature);
    if (page <= 1) {
      next.delete("page");
    } else {
      next.set("page", String(page));
    }
    next.delete("id");
    navigate(next);
  }

  if (!user) {
    return null;
  }

  if (!canView) {
    return (
      <section className={styles.accessState}>
        <p className={styles.eyebrow}>Repositorio normativo</p>
        <h1>Acceso restringido</h1>
        <p>Tu cuenta no tiene permiso para consultar ordenanzas municipales.</p>
      </section>
    );
  }

  const resultNames = new Map<number, string>();
  searchPage?.results.forEach((result) => {
    resultNames.set(result.municipality_id, result.municipality_name);
  });
  comparison?.municipalities.forEach((municipality) => {
    resultNames.set(municipality.id, municipality.name);
  });
  const currentPage = applied.filters.page ?? 1;
  const totalPages = searchPage
    ? Math.max(1, Math.ceil(searchPage.total_matches / ORDINANCE_LIBRARY_PAGE_SIZE))
    : 1;
  const hasAdvancedFilters = Boolean(
    draft.municipalityName ||
      draft.province ||
      draft.topic ||
      draft.populationGte ||
      draft.populationLt ||
      draft.strictTopic ||
      draft.includeInactive ||
      draft.resultScope !== "ordinances" ||
      applied.municipalityId,
  );

  return (
    <section aria-labelledby="ordinance-library-heading" className={styles.library}>
      <header className={styles.hero}>
        <div>
          <p className={styles.eyebrow}>Repositorio normativo municipal</p>
          <h1 id="ordinance-library-heading">Ordenanzas</h1>
          <p className={styles.lead}>
            Busca conceptos en textos jurídicos revisados, abre su ficha y
            contrasta cómo se regula una materia en distintos municipios.
          </p>
        </div>
        <div className={styles.trustMark}>
          <ShieldCheck aria-hidden="true" />
          <span>
            <strong>Corpus supervisado</strong>
            <small>Solo fragmentos aprobados</small>
          </span>
        </div>
      </header>

      <form className={styles.searchPanel} onSubmit={handleSearch}>
        <div className={styles.searchRow}>
          <label className={styles.searchField}>
            <span>¿Qué necesitas localizar?</span>
            <span className={styles.searchInputWrap}>
              <Search aria-hidden="true" />
              <input
                autoComplete="off"
                maxLength={500}
                minLength={2}
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    query: event.target.value,
                  }))
                }
                placeholder="Ej.: terrazas en vía pública, residuos, tasa de agua…"
                type="search"
                value={draft.query}
              />
            </span>
          </label>
          <button className={styles.searchButton} disabled={isSearching} type="submit">
            <Search aria-hidden="true" />
            {isSearching ? "Buscando…" : "Buscar"}
          </button>
        </div>

        {applied.municipalityId ? (
          <div className={styles.contextFilter}>
            <span>
              Consulta limitada al municipio seleccionado desde su ficha
              municipal (ID {applied.municipalityId}).
            </span>
            <button onClick={removeMunicipalityContext} type="button">
              Quitar límite
              <X aria-hidden="true" />
            </button>
          </div>
        ) : null}

        <details
          className={styles.advancedFilters}
          onToggle={(event) =>
            setAdvancedFiltersOpen(event.currentTarget.open)
          }
          open={advancedFiltersOpen}
        >
          <summary>
            <Filter aria-hidden="true" />
            Afinar consulta
            {hasAdvancedFilters ? <span>Filtros activos</span> : null}
          </summary>
          <div className={styles.filterGrid}>
            <label>
              <span>Municipio</span>
              <input
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    municipalityName: event.target.value,
                  }))
                }
                placeholder="Nombre del municipio"
                type="text"
                value={draft.municipalityName}
              />
            </label>
            <label>
              <span>Provincia</span>
              <input
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    province: event.target.value,
                  }))
                }
                placeholder="Ej.: Burgos"
                type="text"
                value={draft.province}
              />
            </label>
            <label>
              <span>Materia</span>
              <input
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    topic: event.target.value,
                  }))
                }
                placeholder="Ej.: medio ambiente"
                type="text"
                value={draft.topic}
              />
            </label>
            <label>
              <span>Organizar resultados</span>
              <select
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    resultScope: event.target.value as OrdinanceResultScope,
                  }))
                }
                value={draft.resultScope}
              >
                {RESULT_SCOPES.map((scope) => (
                  <option key={scope.value} value={scope.value}>
                    {scope.label}
                  </option>
                ))}
              </select>
              <small>
                {
                  RESULT_SCOPES.find((scope) => scope.value === draft.resultScope)
                    ?.description
                }
              </small>
            </label>
            <label>
              <span>Población mínima (incluida)</span>
              <input
                inputMode="numeric"
                min="0"
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    populationGte: event.target.value,
                  }))
                }
                placeholder="Sin mínimo"
                step="1"
                type="number"
                value={draft.populationGte}
              />
            </label>
            <label>
              <span>Población inferior a</span>
              <input
                inputMode="numeric"
                min="0"
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    populationLt: event.target.value,
                  }))
                }
                placeholder="Sin límite"
                step="1"
                type="number"
                value={draft.populationLt}
              />
            </label>
          </div>
          <div className={styles.filterChecks}>
            <label>
              <input
                checked={draft.strictTopic}
                disabled={!draft.topic.trim()}
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    strictTopic: event.target.checked,
                  }))
                }
                type="checkbox"
              />
              Exigir coincidencia exacta de materia
            </label>
            <label className={styles.inactiveCheck}>
              <input
                checked={draft.includeInactive}
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    includeInactive: event.target.checked,
                  }))
                }
                type="checkbox"
              />
              Incluir normas derogadas, sustituidas o archivadas
            </label>
            <button className={styles.clearButton} onClick={clearFilters} type="button">
              Limpiar consulta
            </button>
          </div>
        </details>

        {validationError ? (
          <p className={styles.inlineError} role="alert">
            {validationError}
          </p>
        ) : null}
      </form>

      {canCompare && applied.comparisonMunicipalityIds.length > 0 ? (
        <section className={styles.comparisonSelection}>
          <div>
            <strong>Municipios en comparación</strong>
            <span>
              {applied.comparisonMunicipalityIds.length < 2
                ? "Añade al menos otro municipio."
                : "Comparación actualizada con los filtros jurídicos."}
            </span>
          </div>
          <div className={styles.comparisonChips}>
            {applied.comparisonMunicipalityIds.map((municipalityId) => (
              <button
                key={municipalityId}
                onClick={() => removeComparisonMunicipality(municipalityId)}
                type="button"
              >
                {resultNames.get(municipalityId) ?? `Municipio ${municipalityId}`}
                <X aria-hidden="true" />
              </button>
            ))}
          </div>
          {comparisonNotice ? (
            <p className={styles.inlineError} role="alert">
              {comparisonNotice}
            </p>
          ) : null}
        </section>
      ) : null}

      {!applied.filters.query ? (
        <section className={styles.welcomeState}>
          <BookOpen aria-hidden="true" />
          <div>
            <p className={styles.eyebrow}>Empieza por una materia concreta</p>
            <h2>Consulta por significado, no solo por palabras exactas</h2>
            <p>
              La búsqueda localiza pasajes relacionados en el corpus aprobado.
              No interpreta por sí sola la vigencia ni reemplaza la fuente oficial.
            </p>
            <div className={styles.suggestions}>
              {["ocupación de la vía pública", "gestión de residuos", "ruido y convivencia"].map(
                (suggestion) => (
                  <button
                    key={suggestion}
                    onClick={() => runSuggestedQuery(suggestion)}
                    type="button"
                  >
                    {suggestion}
                  </button>
                ),
              )}
            </div>
          </div>
        </section>
      ) : null}

      {applied.filters.query && applied.filters.query.length < 2 ? (
        <p className={styles.inlineError} role="alert">
          La consulta de la URL debe tener al menos dos caracteres.
        </p>
      ) : null}

      {isSearching ? (
        <section className={styles.searchLoading} aria-live="polite">
          <span />
          <span />
          <span />
          <p>Analizando los fragmentos jurídicos revisados…</p>
        </section>
      ) : null}

      {!isSearching && searchError ? (
        <section className={styles.errorState} role="alert">
          <h2>No se pudo completar la búsqueda</h2>
          <p>{searchError}</p>
        </section>
      ) : null}

      {!isSearching &&
      !searchError &&
      applied.filters.query.length >= 2 &&
      searchPage ? (
        <section className={styles.resultsSection}>
          <p
            aria-live="polite"
            className={styles.srOnly}
            role="status"
          >
            {searchPage.total_matches} coincidencias. Página {currentPage} de {totalPages}.
          </p>
          <header className={styles.resultsHeader}>
            <div>
              <p className={styles.eyebrow}>Resultados revisados</p>
              <h2>
                {new Intl.NumberFormat("es-ES").format(searchPage.total_matches)}{" "}
                {searchPage.total_matches === 1 ? "coincidencia" : "coincidencias"}
              </h2>
            </div>
            <p>
              Página {currentPage} de {totalPages} · consulta “{searchPage.query}”
            </p>
          </header>

          {!searchPage.corpus_scan_complete ? (
            <p className={styles.corpusWarning} role="status">
              La exploración del corpus fue parcial. Refina la consulta antes de
              concluir que no existe regulación comparable.
            </p>
          ) : null}

          {searchPage.population_filter.applied &&
          !searchPage.population_filter.coverage_complete ? (
            <p className={styles.corpusWarning} role="status">
              El filtro poblacional excluye municipios sin dato de población: {" "}
              {searchPage.population_filter.municipalities_without_population} sin
              clasificar en el ámbito elegible.
            </p>
          ) : null}

          {searchPage.legal_status_filter.include_inactive ? (
            <p className={styles.inactiveWarning} role="status">
              Esta consulta incluye normas derogadas, sustituidas o archivadas.
              Revisa el estado de cada ficha antes de usarla.
            </p>
          ) : null}

          <div
            className={`${styles.contentGrid}${
              applied.selectedOrdinanceId ? ` ${styles.contentGridWithDetail}` : ""
            }`}
          >
            <OrdinanceSearchResults
              canCompare={canCompare}
              comparisonMunicipalityIds={applied.comparisonMunicipalityIds}
              onSelect={selectOrdinance}
              onToggleComparison={toggleComparison}
              page={searchPage}
              selectedOrdinanceId={applied.selectedOrdinanceId}
            />
            {applied.selectedOrdinanceId ? (
              <OrdinanceDetailPanel
                detail={detail}
                error={detailError}
                focusKey={applied.selectedOrdinanceId}
                isLoading={isLoadingDetail}
                onClose={closeDetail}
              />
            ) : null}
          </div>

          {totalPages > 1 ? (
            <nav aria-label="Paginación de ordenanzas" className={styles.pagination}>
              <button
                disabled={currentPage <= 1 || isSearching}
                onClick={() => goToPage(currentPage - 1)}
                type="button"
              >
                <ChevronLeft aria-hidden="true" />
                Anterior
              </button>
              <span>
                {currentPage} / {totalPages}
              </span>
              <button
                disabled={!searchPage.has_more || isSearching}
                onClick={() => goToPage(currentPage + 1)}
                type="button"
              >
                Siguiente
                <ChevronRight aria-hidden="true" />
              </button>
            </nav>
          ) : null}
        </section>
      ) : null}

      {applied.selectedOrdinanceId && !searchPage && !isSearching ? (
        <OrdinanceDetailPanel
          detail={detail}
          error={detailError}
          focusKey={applied.selectedOrdinanceId}
          isLoading={isLoadingDetail}
          onClose={closeDetail}
        />
      ) : null}

      {canCompare && applied.comparisonMunicipalityIds.length >= 2 ? (
        <OrdinanceComparisonPanel
          comparison={comparison}
          error={comparisonError}
          isLoading={isLoadingComparison}
          onClose={closeComparison}
          onSelectOrdinance={selectOrdinance}
        />
      ) : null}
    </section>
  );
}
