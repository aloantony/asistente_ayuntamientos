"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import {
  OrdinancesAdmin,
  type OrdinanceFilterValues,
} from "../../../components/OrdinancesAdmin";
import { OrdinanceImportAdmin } from "../../../components/OrdinanceImportAdmin";
import type { Municipality } from "../../../components/types";
import { useOrdinancesAdmin } from "../../../lib/admin/useOrdinancesAdmin";
import { fetchMunicipalityOptions } from "../../../lib/fetchers";
import { useSession } from "../../../lib/session";
import {
  canListMunicipalities,
  canListOrdinances,
  canUseOrdinancesSection,
} from "../nav";

function parsePageParam(value: string | null) {
  const parsed = value ? Number.parseInt(value, 10) : Number.NaN;
  return Number.isInteger(parsed) && parsed > 1 ? parsed : 1;
}

function buildOrdenanzasSearch(filters: OrdinanceFilterValues, page: number) {
  const params = new URLSearchParams();
  if (filters.q) {
    params.set("q", filters.q);
  }
  if (filters.province) {
    params.set("provincia", filters.province);
  }
  if (filters.autonomousCommunity) {
    params.set("comunidad", filters.autonomousCommunity);
  }
  if (filters.topic) {
    params.set("tema", filters.topic);
  }
  if (filters.status) {
    params.set("estado", filters.status);
  }
  if (filters.includeArchived) {
    params.set("archivadas", "1");
  }
  if (page > 1) {
    params.set("page", String(page));
  }

  return params.toString();
}

function AdminOrdenanzasPageInner() {
  const { user, getStoredToken, handleRequestError } = useSession();
  const router = useRouter();
  const searchParams = useSearchParams();
  const canUseOrdinances = Boolean(user && canUseOrdinancesSection(user));
  // El backend exige view||manage para listar; los usuarios solo-crear
  // conservan la página y el formulario sin disparar peticiones de listado.
  const canList = Boolean(user && canListOrdinances(user));
  const ordinancesAdmin = useOrdinancesAdmin({
    getStoredToken,
    handleRequestError,
    canList,
  });
  const [municipalities, setMunicipalities] = useState<Municipality[]>([]);
  // Fallo de la lista de referencia de municipios; sin ella el selector del
  // formulario queda vacío, así que el error se muestra como en el resto de
  // páginas de admin.
  const [referenceError, setReferenceError] = useState("");
  // El backend exige municipalities.view||manage para listar municipios: sin
  // ese permiso no se dispara una petición condenada al 403 (el selector cae
  // a los municipios derivados de las ordenanzas listadas).
  const canLoadMunicipalities = Boolean(user && canListMunicipalities(user));

  // La URL es la única fuente de verdad de filtros y página.
  const urlFilters: OrdinanceFilterValues = {
    q: searchParams.get("q") ?? "",
    province: searchParams.get("provincia") ?? "",
    autonomousCommunity: searchParams.get("comunidad") ?? "",
    topic: searchParams.get("tema") ?? "",
    status: searchParams.get("estado") ?? "",
    includeArchived: searchParams.get("archivadas") === "1",
  };
  const page = parsePageParam(searchParams.get("page"));

  useEffect(() => {
    if (!canList) {
      return;
    }

    void ordinancesAdmin.loadOrdinances({ ...urlFilters, page });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    user?.id,
    user?.permissions,
    urlFilters.q,
    urlFilters.province,
    urlFilters.autonomousCommunity,
    urlFilters.topic,
    urlFilters.status,
    urlFilters.includeArchived,
    page,
  ]);

  // Clamp de páginas fuera de rango (marcador antiguo o total reducido tras
  // archivar): si la carga terminó vacía pero hay resultados, se normaliza a
  // la última página válida. Solo puede dispararse de nuevo tras otra carga
  // vacía con una página aún mayor, así que no entra en bucle.
  useEffect(() => {
    if (
      !ordinancesAdmin.isLoadingOrdinances &&
      ordinancesAdmin.ordinances.length === 0 &&
      ordinancesAdmin.ordinanceTotal > 0 &&
      page > 1 &&
      page > pageCount
    ) {
      replaceSearch(buildOrdenanzasSearch(urlFilters, pageCount));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    ordinancesAdmin.isLoadingOrdinances,
    ordinancesAdmin.ordinances.length,
    ordinancesAdmin.ordinanceTotal,
    page,
  ]);

  useEffect(() => {
    if (!canUseOrdinances || !canLoadMunicipalities) {
      return;
    }

    let isActive = true;
    setReferenceError("");

    fetchMunicipalityOptions()
      .then((municipalitiesData) => {
        if (isActive) {
          setMunicipalities(municipalitiesData);
        }
      })
      .catch((referenceFetchError) => {
        if (isActive) {
          handleRequestError(
            referenceFetchError,
            setReferenceError,
            "No se pudo cargar la lista de municipios.",
          );
        }
      });

    return () => {
      isActive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id, user?.permissions]);

  function buildHref(search: string) {
    return search ? `/admin/ordenanzas?${search}` : "/admin/ordenanzas";
  }

  // replace: normalizaciones automáticas (clamp); push: acciones del usuario
  // (filtros, paginación), para que atrás/adelante las recorra.
  function replaceSearch(search: string) {
    router.replace(buildHref(search), { scroll: false });
  }

  function pushSearch(search: string) {
    router.push(buildHref(search), { scroll: false });
  }

  function handleApplyFilters(filters: OrdinanceFilterValues) {
    const unchanged =
      page === 1 &&
      filters.q === urlFilters.q &&
      filters.province === urlFilters.province &&
      filters.autonomousCommunity === urlFilters.autonomousCommunity &&
      filters.topic === urlFilters.topic &&
      filters.status === urlFilters.status &&
      filters.includeArchived === urlFilters.includeArchived;
    if (unchanged) {
      // La URL no cambiaría, así que recargamos directamente.
      void ordinancesAdmin.loadOrdinances({ ...filters, page: 1 });
      return;
    }

    pushSearch(buildOrdenanzasSearch(filters, 1));
  }

  const pageCount = Math.max(
    1,
    Math.ceil(ordinancesAdmin.ordinanceTotal / ordinancesAdmin.adminPageSize),
  );

  function handlePrevPage() {
    if (page > 1) {
      pushSearch(buildOrdenanzasSearch(urlFilters, page - 1));
    }
  }

  function handleNextPage() {
    if (page < pageCount) {
      pushSearch(buildOrdenanzasSearch(urlFilters, page + 1));
    }
  }

  if (!user || !canUseOrdinances) {
    return (
      <section className="panel">
        <p className="eyebrow">Administración</p>
        <h2>Acceso restringido</h2>
        <p className="muted">No tienes permisos de administración.</p>
      </section>
    );
  }

  return (
    <section className="panel admin-panel">
      <div className="panel-header">
        <div>
          <h2>Ordenanzas</h2>
        </div>
        {canList ? (
          <button
            className="secondary-button"
            type="button"
            onClick={() =>
              void ordinancesAdmin.loadOrdinances({ ...urlFilters, page })
            }
            disabled={ordinancesAdmin.isLoadingOrdinances}
          >
            {ordinancesAdmin.isLoadingOrdinances ? "Cargando..." : "Actualizar"}
          </button>
        ) : null}
      </div>

      {ordinancesAdmin.ordinancesError || referenceError ? (
        <p className="error-message">
          {ordinancesAdmin.ordinancesError || referenceError}
        </p>
      ) : null}

      <OrdinancesAdmin
        currentUser={user}
        municipalities={municipalities}
        ordinances={ordinancesAdmin.ordinances}
        isLoadingAdmin={ordinancesAdmin.isLoadingOrdinances}
        ordinancePage={page - 1}
        ordinanceTotal={ordinancesAdmin.ordinanceTotal}
        ordinancePageSize={ordinancesAdmin.adminPageSize}
        ordinanceSearchText={urlFilters.q}
        ordinanceProvinceFilter={urlFilters.province}
        ordinanceAutonomousCommunityFilter={urlFilters.autonomousCommunity}
        ordinanceTopicFilter={urlFilters.topic}
        ordinanceStatusFilter={urlFilters.status}
        ordinanceIncludeArchived={urlFilters.includeArchived}
        newOrdinance={ordinancesAdmin.newOrdinance}
        ordinanceFormError={ordinancesAdmin.ordinanceFormError}
        isCreatingOrdinance={ordinancesAdmin.isCreatingOrdinance}
        ordinanceEdits={ordinancesAdmin.ordinanceEdits}
        ordinanceEditError={ordinancesAdmin.ordinanceEditError}
        ordinanceEditMessage={ordinancesAdmin.ordinanceEditMessage}
        updatingOrdinanceId={ordinancesAdmin.updatingOrdinanceId}
        ordinanceDetailLoaded={ordinancesAdmin.ordinanceDetailLoaded}
        loadingOrdinanceDetailId={ordinancesAdmin.loadingOrdinanceDetailId}
        onApplyOrdinanceFilters={handleApplyFilters}
        onOrdinancePrevPage={handlePrevPage}
        onOrdinanceNextPage={handleNextPage}
        onUpdateNewOrdinance={ordinancesAdmin.updateNewOrdinance}
        onCreateOrdinance={ordinancesAdmin.handleCreateOrdinance}
        onStartOrdinanceEdit={ordinancesAdmin.handleStartOrdinanceEdit}
        onUpdateOrdinanceEdit={ordinancesAdmin.updateOrdinanceEdit}
        onUpdateOrdinance={ordinancesAdmin.handleUpdateOrdinance}
        onArchiveOrdinance={ordinancesAdmin.handleArchiveOrdinance}
      />
      <OrdinanceImportAdmin municipalities={municipalities} />
    </section>
  );
}

export default function AdminOrdenanzasPage() {
  return (
    <Suspense
      fallback={
        <section className="panel">
          <p className="muted">Cargando…</p>
        </section>
      }
    >
      <AdminOrdenanzasPageInner />
    </Suspense>
  );
}
