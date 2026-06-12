"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect } from "react";
import {
  MunicipalitiesAdmin,
  type MunicipalityFilterValues,
} from "../../../components/MunicipalitiesAdmin";
import { useMunicipalitiesAdmin } from "../../../lib/admin/useMunicipalitiesAdmin";
import { useSession } from "../../../lib/session";
import { canListMunicipalities, canUseMunicipalitiesSection } from "../nav";

function parsePageParam(value: string | null) {
  const parsed = value ? Number.parseInt(value, 10) : Number.NaN;
  return Number.isInteger(parsed) && parsed > 1 ? parsed : 1;
}

function buildMunicipiosSearch(
  filters: MunicipalityFilterValues,
  page: number,
) {
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

function AdminMunicipiosPageInner() {
  const { user, getStoredToken, handleRequestError } = useSession();
  const router = useRouter();
  const searchParams = useSearchParams();
  const canUseMunicipalities = Boolean(
    user && canUseMunicipalitiesSection(user),
  );
  // El backend exige view||manage para listar; los usuarios solo-crear
  // conservan la página y el formulario sin disparar peticiones de listado.
  const canList = Boolean(user && canListMunicipalities(user));
  const municipalitiesAdmin = useMunicipalitiesAdmin({
    getStoredToken,
    handleRequestError,
    canList,
  });

  // La URL es la única fuente de verdad de filtros y página.
  const urlFilters: MunicipalityFilterValues = {
    q: searchParams.get("q") ?? "",
    province: searchParams.get("provincia") ?? "",
    autonomousCommunity: searchParams.get("comunidad") ?? "",
    status: searchParams.get("estado") ?? "",
    includeArchived: searchParams.get("archivadas") === "1",
  };
  const page = parsePageParam(searchParams.get("page"));

  useEffect(() => {
    if (!canList) {
      return;
    }

    void municipalitiesAdmin.loadMunicipalities({ ...urlFilters, page });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    user?.id,
    user?.permissions,
    urlFilters.q,
    urlFilters.province,
    urlFilters.autonomousCommunity,
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
      !municipalitiesAdmin.isLoadingMunicipalities &&
      municipalitiesAdmin.municipalities.length === 0 &&
      municipalitiesAdmin.municipalityTotal > 0 &&
      page > 1 &&
      page > pageCount
    ) {
      replaceSearch(buildMunicipiosSearch(urlFilters, pageCount));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    municipalitiesAdmin.isLoadingMunicipalities,
    municipalitiesAdmin.municipalities.length,
    municipalitiesAdmin.municipalityTotal,
    page,
  ]);

  function buildHref(search: string) {
    return search ? `/admin/municipios?${search}` : "/admin/municipios";
  }

  // replace: normalizaciones automáticas (clamp); push: acciones del usuario
  // (filtros, paginación), para que atrás/adelante las recorra.
  function replaceSearch(search: string) {
    router.replace(buildHref(search), { scroll: false });
  }

  function pushSearch(search: string) {
    router.push(buildHref(search), { scroll: false });
  }

  function handleApplyFilters(filters: MunicipalityFilterValues) {
    const unchanged =
      page === 1 &&
      filters.q === urlFilters.q &&
      filters.province === urlFilters.province &&
      filters.autonomousCommunity === urlFilters.autonomousCommunity &&
      filters.status === urlFilters.status &&
      filters.includeArchived === urlFilters.includeArchived;
    if (unchanged) {
      // La URL no cambiaría, así que recargamos directamente.
      void municipalitiesAdmin.loadMunicipalities({ ...filters, page: 1 });
      return;
    }

    pushSearch(buildMunicipiosSearch(filters, 1));
  }

  const pageCount = Math.max(
    1,
    Math.ceil(
      municipalitiesAdmin.municipalityTotal / municipalitiesAdmin.adminPageSize,
    ),
  );

  function handlePrevPage() {
    if (page > 1) {
      pushSearch(buildMunicipiosSearch(urlFilters, page - 1));
    }
  }

  function handleNextPage() {
    if (page < pageCount) {
      pushSearch(buildMunicipiosSearch(urlFilters, page + 1));
    }
  }

  if (!user || !canUseMunicipalities) {
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
          <h2>Municipios</h2>
        </div>
        {canList ? (
          <button
            className="secondary-button"
            type="button"
            onClick={() =>
              void municipalitiesAdmin.loadMunicipalities({
                ...urlFilters,
                page,
              })
            }
            disabled={municipalitiesAdmin.isLoadingMunicipalities}
          >
            {municipalitiesAdmin.isLoadingMunicipalities
              ? "Cargando..."
              : "Actualizar"}
          </button>
        ) : null}
      </div>

      {municipalitiesAdmin.municipalitiesError ? (
        <p className="error-message">
          {municipalitiesAdmin.municipalitiesError}
        </p>
      ) : null}

      <MunicipalitiesAdmin
        currentUser={user}
        municipalities={municipalitiesAdmin.municipalities}
        isLoadingAdmin={municipalitiesAdmin.isLoadingMunicipalities}
        municipalityPage={page - 1}
        municipalityTotal={municipalitiesAdmin.municipalityTotal}
        municipalityPageSize={municipalitiesAdmin.adminPageSize}
        municipalitySearchText={urlFilters.q}
        municipalityProvinceFilter={urlFilters.province}
        municipalityAutonomousCommunityFilter={urlFilters.autonomousCommunity}
        municipalityStatusFilter={urlFilters.status}
        municipalityIncludeArchived={urlFilters.includeArchived}
        newMunicipality={municipalitiesAdmin.newMunicipality}
        municipalityFormError={municipalitiesAdmin.municipalityFormError}
        isCreatingMunicipality={municipalitiesAdmin.isCreatingMunicipality}
        municipalityEdits={municipalitiesAdmin.municipalityEdits}
        municipalityEditError={municipalitiesAdmin.municipalityEditError}
        municipalityEditMessage={municipalitiesAdmin.municipalityEditMessage}
        updatingMunicipalityId={municipalitiesAdmin.updatingMunicipalityId}
        onApplyMunicipalityFilters={handleApplyFilters}
        onMunicipalityPrevPage={handlePrevPage}
        onMunicipalityNextPage={handleNextPage}
        onUpdateNewMunicipality={municipalitiesAdmin.updateNewMunicipality}
        onCreateMunicipality={municipalitiesAdmin.handleCreateMunicipality}
        onUpdateMunicipalityEdit={municipalitiesAdmin.updateMunicipalityEdit}
        onUpdateMunicipality={municipalitiesAdmin.handleUpdateMunicipality}
        onArchiveMunicipality={municipalitiesAdmin.handleArchiveMunicipality}
      />
    </section>
  );
}

export default function AdminMunicipiosPage() {
  return (
    <Suspense
      fallback={
        <section className="panel">
          <p className="muted">Cargando…</p>
        </section>
      }
    >
      <AdminMunicipiosPageInner />
    </Suspense>
  );
}
