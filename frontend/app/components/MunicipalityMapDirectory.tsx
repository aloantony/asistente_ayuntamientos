"use client";

import {
  Building2,
  FileText,
  MapPinOff,
  RefreshCw,
  Search,
} from "lucide-react";
import { useEffect, useId, useState } from "react";
import { adminRequestWithTotal } from "../lib/api";
import { fetchOrdinancesTotal } from "../lib/fetchers";
import {
  formatMunicipalityType,
  formatRuralUrbanProfile,
  userHasPermission,
  type Municipality,
  type User,
} from "./types";
import styles from "./MunicipalityMapDirectory.module.css";

const SEARCH_DEBOUNCE_MS = 300;
const MUNICIPALITY_LIMIT = 200;
const numberFormatter = new Intl.NumberFormat("es-ES");
const decimalFormatter = new Intl.NumberFormat("es-ES", {
  maximumFractionDigits: 2,
});

type MunicipalityMapDirectoryProps = {
  user: User;
};

function isAbortError(error: unknown) {
  return error instanceof DOMException && error.name === "AbortError";
}

function requestErrorMessage(error: unknown, fallback: string) {
  return error instanceof Error && error.message ? error.message : fallback;
}

function formatOptionalNumber(value: number | null, suffix = "") {
  return value === null ? "No disponible" : `${decimalFormatter.format(value)}${suffix}`;
}

function municipalityLocation(municipality: Municipality) {
  return `${municipality.province} · ${municipality.autonomous_community}`;
}

export function MunicipalityMapDirectory({
  user,
}: MunicipalityMapDirectoryProps) {
  const searchId = useId();
  const archivedId = useId();
  const selectorId = useId();
  const [query, setQuery] = useState("");
  const [includeArchived, setIncludeArchived] = useState(false);
  const [municipalities, setMunicipalities] = useState<Municipality[]>([]);
  const [municipalityTotal, setMunicipalityTotal] = useState(0);
  const [selectedMunicipalityId, setSelectedMunicipalityId] = useState<
    number | null
  >(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState("");
  const [reloadKey, setReloadKey] = useState(0);
  const [ordinanceTotal, setOrdinanceTotal] = useState<number | null>(null);
  const [isLoadingOrdinances, setIsLoadingOrdinances] = useState(false);
  const [ordinancesError, setOrdinancesError] = useState("");

  const canViewMunicipalities =
    userHasPermission(user, "municipalities.view") ||
    userHasPermission(user, "municipalities.manage");
  const canViewOrdinances =
    userHasPermission(user, "ordinances.view") ||
    userHasPermission(user, "ordinances.manage");
  const selectedMunicipality =
    municipalities.find(
      (municipality) => municipality.id === selectedMunicipalityId,
    ) ?? null;

  useEffect(() => {
    if (!canViewMunicipalities) {
      return;
    }

    const controller = new AbortController();
    const debounceId = window.setTimeout(async () => {
      setIsLoading(true);
      setError("");

      const params = new URLSearchParams({
        q: query.trim(),
        include_archived: String(includeArchived),
        limit: String(MUNICIPALITY_LIMIT),
      });

      try {
        const { items, total } = await adminRequestWithTotal<Municipality[]>(
          `/municipalities?${params.toString()}`,
          "",
          "No se pudo cargar el directorio municipal.",
          { signal: controller.signal },
        );

        setMunicipalities(items);
        setMunicipalityTotal(total);
        setSelectedMunicipalityId((currentId) =>
          currentId !== null &&
          items.some((municipality) => municipality.id === currentId)
            ? currentId
            : (items[0]?.id ?? null),
        );
      } catch (requestError) {
        if (!isAbortError(requestError)) {
          setMunicipalities([]);
          setMunicipalityTotal(0);
          setSelectedMunicipalityId(null);
          setError(
            requestErrorMessage(
              requestError,
              "No se pudo cargar el directorio municipal.",
            ),
          );
        }
      } finally {
        if (!controller.signal.aborted) {
          setIsLoading(false);
        }
      }
    }, SEARCH_DEBOUNCE_MS);

    return () => {
      window.clearTimeout(debounceId);
      controller.abort();
    };
  }, [canViewMunicipalities, includeArchived, query, reloadKey]);

  useEffect(() => {
    if (!canViewOrdinances || selectedMunicipalityId === null) {
      setOrdinanceTotal(null);
      setIsLoadingOrdinances(false);
      setOrdinancesError("");
      return;
    }

    let isCurrentSelection = true;
    setOrdinanceTotal(null);
    setIsLoadingOrdinances(true);
    setOrdinancesError("");

    void fetchOrdinancesTotal(selectedMunicipalityId)
      .then((total) => {
        if (isCurrentSelection) {
          setOrdinanceTotal(total);
        }
      })
      .catch((requestError: unknown) => {
        if (isCurrentSelection) {
          setOrdinancesError(
            requestErrorMessage(
              requestError,
              "No se pudo consultar el recuento de ordenanzas.",
            ),
          );
        }
      })
      .finally(() => {
        if (isCurrentSelection) {
          setIsLoadingOrdinances(false);
        }
      });

    return () => {
      isCurrentSelection = false;
    };
  }, [canViewOrdinances, selectedMunicipalityId]);

  if (!canViewMunicipalities) {
    return (
      <section className={styles.accessState}>
        <Building2 aria-hidden="true" />
        <div>
          <p className={styles.eyebrow}>Directorio territorial</p>
          <h2>Acceso restringido</h2>
          <p>
            Necesitas el permiso de consulta de municipios para utilizar esta
            vista.
          </p>
        </div>
      </section>
    );
  }

  return (
    <section className={styles.directory}>
      <header className={styles.header}>
        <div>
          <p className={styles.eyebrow}>Directorio territorial</p>
          <h2>Municipios disponibles</h2>
          <p className={styles.lead}>
            Localiza un municipio por nombre, provincia o comunidad autónoma y
            consulta su contexto antes de trabajar con su normativa.
          </p>
        </div>
        <div className={styles.total} aria-live="polite">
          <strong>{numberFormatter.format(municipalityTotal)}</strong>
          <span>{municipalityTotal === 1 ? "municipio" : "municipios"}</span>
        </div>
      </header>

      <div className={styles.toolbar}>
        <label className={styles.searchField} htmlFor={searchId}>
          <span>Buscar municipios</span>
          <span className={styles.searchControl}>
            <Search aria-hidden="true" />
            <input
              id={searchId}
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Nombre, provincia o comunidad autónoma"
              autoComplete="off"
            />
          </span>
        </label>

        <label className={styles.archiveToggle} htmlFor={archivedId}>
          <input
            id={archivedId}
            type="checkbox"
            checked={includeArchived}
            onChange={(event) => setIncludeArchived(event.target.checked)}
          />
          Incluir municipios archivados
        </label>
      </div>

      {error ? (
        <div className={styles.errorState} role="alert">
          <div>
            <strong>No se ha podido cargar el directorio</strong>
            <p>{error}</p>
          </div>
          <button type="button" onClick={() => setReloadKey((key) => key + 1)}>
            <RefreshCw aria-hidden="true" />
            Reintentar
          </button>
        </div>
      ) : null}

      <div className={styles.workspace}>
        <aside className={styles.listPanel} aria-busy={isLoading}>
          <div className={styles.panelHeading}>
            <div>
              <span>Resultados</span>
              <strong>
                {isLoading
                  ? "Actualizando…"
                  : `${municipalities.length} mostrados`}
              </strong>
            </div>
            {municipalityTotal > MUNICIPALITY_LIMIT ? (
              <small>Afina la búsqueda para ver otros resultados.</small>
            ) : null}
          </div>

          {isLoading && municipalities.length === 0 ? (
            <div className={styles.loadingState} role="status">
              <span className={styles.spinner} aria-hidden="true" />
              Cargando municipios…
            </div>
          ) : null}

          {!isLoading && !error && municipalities.length === 0 ? (
            <div className={styles.emptyState}>
              <Search aria-hidden="true" />
              <strong>No hay coincidencias</strong>
              <p>Prueba con otro nombre, provincia o comunidad autónoma.</p>
            </div>
          ) : null}

          {municipalities.length > 0 ? (
            <label className={styles.selectorLabel} htmlFor={selectorId}>
              <span className={styles.visuallyHidden}>
                Seleccionar municipio
              </span>
              <select
                id={selectorId}
                className={styles.selector}
                size={Math.min(Math.max(municipalities.length, 5), 12)}
                value={selectedMunicipalityId ?? ""}
                onChange={(event) =>
                  setSelectedMunicipalityId(Number(event.target.value))
                }
                aria-describedby={`${selectorId}-help`}
              >
                {municipalities.map((municipality) => (
                  <option key={municipality.id} value={municipality.id}>
                    {municipality.name} — {municipalityLocation(municipality)}
                    {municipality.status === "archived" ? " (archivado)" : ""}
                  </option>
                ))}
              </select>
              <small id={`${selectorId}-help`}>
                Usa las flechas para recorrer el listado.
              </small>
            </label>
          ) : null}
        </aside>

        <div className={styles.detailPanel}>
          {selectedMunicipality ? (
            <>
              <div className={styles.detailHeader}>
                <div>
                  <p>{municipalityLocation(selectedMunicipality)}</p>
                  <h3>{selectedMunicipality.name}</h3>
                </div>
                <span
                  className={styles.status}
                  data-archived={selectedMunicipality.status === "archived"}
                >
                  {selectedMunicipality.status === "archived"
                    ? "Archivado"
                    : "Activo"}
                </span>
              </div>

              <dl className={styles.metrics}>
                <div>
                  <dt>Población</dt>
                  <dd>
                    {selectedMunicipality.population === null
                      ? "No disponible"
                      : numberFormatter.format(selectedMunicipality.population)}
                  </dd>
                  <span>
                    {selectedMunicipality.population_reference_year
                      ? `Referencia ${selectedMunicipality.population_reference_year}`
                      : "Año de referencia no indicado"}
                  </span>
                </div>
                <div>
                  <dt>Superficie</dt>
                  <dd>
                    {formatOptionalNumber(
                      selectedMunicipality.surface_km2,
                      " km²",
                    )}
                  </dd>
                </div>
                <div>
                  <dt>Densidad</dt>
                  <dd>
                    {formatOptionalNumber(
                      selectedMunicipality.density,
                      " hab./km²",
                    )}
                  </dd>
                </div>
                {canViewOrdinances ? (
                  <div>
                    <dt>Ordenanzas</dt>
                    <dd>
                      {isLoadingOrdinances
                        ? "Consultando…"
                        : ordinancesError
                          ? "No disponible"
                          : numberFormatter.format(ordinanceTotal ?? 0)}
                    </dd>
                    {ordinancesError ? (
                      <span className={styles.metricError}>{ordinancesError}</span>
                    ) : (
                      <span>Registros asociados al municipio</span>
                    )}
                  </div>
                ) : null}
              </dl>

              <div className={styles.detailGrid}>
                <section className={styles.infoCard}>
                  <div className={styles.cardHeading}>
                    <Building2 aria-hidden="true" />
                    <h4>Perfil municipal</h4>
                  </div>
                  <dl className={styles.profileList}>
                    <div>
                      <dt>Entidad</dt>
                      <dd>
                        {formatMunicipalityType(
                          selectedMunicipality.municipality_type,
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>Entorno</dt>
                      <dd>
                        {formatRuralUrbanProfile(
                          selectedMunicipality.rural_urban_profile,
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>Perfil económico</dt>
                      <dd>
                        {selectedMunicipality.economic_profile || "Sin datos"}
                      </dd>
                    </div>
                    <div>
                      <dt>Perfil turístico</dt>
                      <dd>
                        {selectedMunicipality.tourism_profile || "Sin datos"}
                      </dd>
                    </div>
                  </dl>
                </section>

                <section className={styles.infoCard}>
                  <div className={styles.cardHeading}>
                    <FileText aria-hidden="true" />
                    <h4>Notas de contexto</h4>
                  </div>
                  <div className={styles.notes}>
                    <div>
                      <strong>Geográficas</strong>
                      <p>
                        {selectedMunicipality.geographic_notes ||
                          "Sin notas geográficas."}
                      </p>
                    </div>
                    <div>
                      <strong>Administrativas</strong>
                      <p>
                        {selectedMunicipality.administrative_notes ||
                          "Sin notas administrativas."}
                      </p>
                    </div>
                  </div>
                </section>
              </div>

              <div className={styles.mapNotice}>
                <MapPinOff aria-hidden="true" />
                <div>
                  <strong>Ubicación cartográfica pendiente</strong>
                  <p>
                    El centroide municipal aún no está disponible en los datos
                    cartográficos. La ficha muestra únicamente información
                    territorial verificada.
                  </p>
                </div>
              </div>
            </>
          ) : (
            <div className={styles.detailEmpty}>
              <Building2 aria-hidden="true" />
              <strong>Selecciona un municipio</strong>
              <p>Su ficha territorial aparecerá en este espacio.</p>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
