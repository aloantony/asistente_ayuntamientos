"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { fetchMunicipality } from "../lib/fetchers";
import { canViewMunicipalHub } from "../lib/permissions";
import {
  shouldShowProjectsPanel,
  shouldShowRequirementsPanel,
  useSession,
} from "../lib/session";
import type {
  Municipality,
  MunicipalitySummary,
  OrganizationSummary,
  User,
} from "./types";
import { userHasPermission } from "./types";

type MunicipalContext = {
  organization: OrganizationSummary;
  municipality: MunicipalitySummary;
};

type ModuleLink = {
  href: string;
  label: string;
  description: string;
};

const MUNICIPALITY_TYPE_LABELS: Record<
  Municipality["municipality_type"],
  string
> = {
  municipality: "Municipio",
  minor_local_entity: "Entidad local menor",
  district: "Distrito",
  other: "Otro",
};

const RURAL_URBAN_PROFILE_LABELS: Record<
  Municipality["rural_urban_profile"],
  string
> = {
  rural: "Rural",
  semi_rural: "Semirrural",
  urban: "Urbano",
  mixed: "Mixto",
  unknown: "Sin clasificar",
};

function formatInteger(value: number | null) {
  return value === null
    ? "No consta"
    : new Intl.NumberFormat("es-ES", { maximumFractionDigits: 0 }).format(
        value,
      );
}

function formatDecimal(value: number | null, suffix: string) {
  if (value === null) {
    return "No consta";
  }

  return `${new Intl.NumberFormat("es-ES", {
    maximumFractionDigits: 2,
  }).format(value)} ${suffix}`;
}

function formatUpdatedAt(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "Fecha no disponible";
  }

  return new Intl.DateTimeFormat("es-ES", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Europe/Madrid",
  }).format(date);
}

function getMunicipalContexts(user: User) {
  return (user.organizations ?? [])
    .flatMap<MunicipalContext>((organization) => {
      if (organization.status === "archived" || !organization.municipality) {
        return [];
      }

      return [
        {
          organization,
          municipality: organization.municipality,
        },
      ];
    })
    .sort((left, right) => {
      if (left.organization.status !== right.organization.status) {
        return left.organization.status === "active" ? -1 : 1;
      }

      const organizationOrder = left.organization.name.localeCompare(
        right.organization.name,
        "es",
      );
      return organizationOrder || left.organization.id - right.organization.id;
    });
}

function getModuleLinks(user: User, municipality: Municipality) {
  const links: ModuleLink[] = [];

  if (
    userHasPermission(user, "map.view") ||
    userHasPermission(user, "map.manage")
  ) {
    links.push({
      href: "/mapa",
      label: "Mapa",
      description: "Consulta la información geolocalizada disponible.",
    });
  }

  if (shouldShowRequirementsPanel(user)) {
    links.push({
      href: "/requisitos",
      label: "Necesidades",
      description: "Revisa y organiza las necesidades municipales.",
    });
  }

  if (shouldShowProjectsPanel(user)) {
    links.push({
      href: "/proyectos",
      label: "Proyectos",
      description: "Accede a los proyectos permitidos para tu cuenta.",
    });
  }

  if (userHasPermission(user, "assistant.use")) {
    links.push({
      href: "/asistente",
      label: "Anacleto",
      description: "Consulta al asistente con las herramientas autorizadas.",
    });
  }

  if (
    [
      "ordinances.create",
      "ordinances.edit",
      "ordinances.archive",
      "ordinances.import",
      "ordinances.review",
      "ordinances.manage",
    ].some((permissionCode) => userHasPermission(user, permissionCode))
  ) {
    links.push({
      href: "/admin/ordenanzas",
      label: "Ordenanzas",
      description: "Gestiona el repositorio normativo disponible.",
    });
  }

  if (
    [
      "municipalities.create",
      "municipalities.edit",
      "municipalities.archive",
      "municipalities.manage",
    ].some((permissionCode) => userHasPermission(user, permissionCode))
  ) {
    links.push({
      href: `/admin/municipios?q=${encodeURIComponent(municipality.name)}`,
      label: "Gestionar ficha",
      description: "Abre la ficha administrativa de este municipio.",
    });
  }

  return links;
}

function RestrictedState() {
  return (
    <section className="panel municipal-hub-state">
      <p className="eyebrow">Ayuntamiento</p>
      <h1>Acceso restringido</h1>
      <p className="muted">
        Esta sección no está disponible para esta cuenta.
      </p>
    </section>
  );
}

function EmptyState({ user }: { user: User }) {
  const organizations = user.organizations ?? [];
  const hasEligibleOrganizations = organizations.some(
    (organization) => organization.status !== "archived",
  );
  const canManageOrganizations = userHasPermission(user, "organizations.manage");

  let message = "Tu cuenta todavía no pertenece a ninguna organización municipal.";
  if (organizations.length > 0 && !hasEligibleOrganizations) {
    message = "Las organizaciones afiliadas a tu cuenta están archivadas.";
  } else if (hasEligibleOrganizations) {
    message =
      "Tus organizaciones activas o pausadas no tienen un municipio vinculado.";
  }

  return (
    <section className="panel municipal-hub-state">
      <p className="eyebrow">Ayuntamiento</p>
      <h1>Sin municipio afiliado</h1>
      <p className="muted">{message}</p>
      {canManageOrganizations ? (
        <Link className="municipal-hub-state-action" href="/admin/organizaciones">
          Revisar organizaciones
        </Link>
      ) : null}
    </section>
  );
}

export function MunicipalHub() {
  const { user, handleRequestError } = useSession();
  const [selectedOrganizationId, setSelectedOrganizationId] = useState<
    number | null
  >(null);
  const [municipality, setMunicipality] = useState<Municipality | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState("");
  const [loadAttempt, setLoadAttempt] = useState(0);
  const requestSequenceRef = useRef(0);

  const contexts = user ? getMunicipalContexts(user) : [];
  const permissionSignature = (user?.permissions ?? []).slice().sort().join(",");
  const selectedContext =
    contexts.find(
      ({ organization }) => organization.id === selectedOrganizationId,
    ) ??
    contexts[0] ??
    null;

  useEffect(() => {
    if (!user || !canViewMunicipalHub(user) || !selectedContext) {
      requestSequenceRef.current += 1;
      setMunicipality(null);
      setError("");
      setIsLoading(false);
      return;
    }

    const controller = new AbortController();
    const requestSequence = ++requestSequenceRef.current;
    const municipalityId = selectedContext.municipality.id;
    setMunicipality(null);
    setError("");
    setIsLoading(true);

    fetchMunicipality(municipalityId, controller.signal)
      .then((loadedMunicipality) => {
        if (
          !controller.signal.aborted &&
          requestSequence === requestSequenceRef.current
        ) {
          setMunicipality(loadedMunicipality);
        }
      })
      .catch((loadError) => {
        if (
          controller.signal.aborted ||
          requestSequence !== requestSequenceRef.current
        ) {
          return;
        }

        handleRequestError(
          loadError,
          setError,
          "No se pudo cargar la información municipal.",
        );
      })
      .finally(() => {
        if (
          !controller.signal.aborted &&
          requestSequence === requestSequenceRef.current
        ) {
          setIsLoading(false);
        }
      });

    return () => controller.abort();
    // La selección y la firma de permisos delimitan la petición; abortar y
    // secuenciar impide que una respuesta anterior reemplace la nueva.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    user?.id,
    user?.is_superuser,
    selectedContext?.organization.id,
    selectedContext?.municipality.id,
    permissionSignature,
    loadAttempt,
  ]);

  if (!user) {
    return null;
  }

  if (!canViewMunicipalHub(user)) {
    return <RestrictedState />;
  }

  if (!selectedContext) {
    return <EmptyState user={user} />;
  }

  const moduleLinks = municipality ? getModuleLinks(user, municipality) : [];
  const isPaused = selectedContext.organization.status === "paused";

  return (
    <section className="municipal-hub">
      <header className="municipal-hub-hero">
        <div className="municipal-hub-title">
          <p className="eyebrow">Ayuntamiento</p>
          <h1>{selectedContext.municipality.name}</h1>
          <p className="municipal-hub-location">
            {selectedContext.municipality.province} ·{" "}
            {selectedContext.municipality.autonomous_community}
          </p>
        </div>

        {contexts.length > 1 ? (
          <label className="municipal-hub-selector">
            <span>Organización y municipio</span>
            <select
              onChange={(event) =>
                setSelectedOrganizationId(Number(event.target.value))
              }
              value={selectedContext.organization.id}
            >
              {contexts.map(({ organization, municipality: summary }) => (
                <option key={organization.id} value={organization.id}>
                  {organization.name} · {summary.name}
                  {organization.status === "paused" ? " (pausada)" : ""}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <p className="municipal-hub-affiliation">
            Organización: {selectedContext.organization.name}
          </p>
        )}
      </header>

      {isPaused ? (
        <p className="municipal-hub-warning" role="status">
          Esta organización está pausada. La ficha se muestra en modo de
          consulta.
        </p>
      ) : null}

      <div aria-live="polite" className="municipal-hub-content">
        {isLoading ? (
          <div aria-busy="true" className="municipal-hub-loading">
            <p className="muted">Cargando la ficha municipal…</p>
            <div aria-hidden="true" className="municipal-hub-skeleton-grid">
              <span />
              <span />
              <span />
              <span />
            </div>
          </div>
        ) : null}

        {!isLoading && error ? (
          <div className="panel municipal-hub-load-error" role="alert">
            <h2>No se pudo cargar la ficha</h2>
            <p className="error-message">{error}</p>
            <button
              type="button"
              onClick={() => setLoadAttempt((value) => value + 1)}
            >
              Reintentar
            </button>
          </div>
        ) : null}

        {!isLoading && !error && municipality ? (
          <>
            {municipality.status === "archived" ? (
              <p className="municipal-hub-warning" role="status">
                Este municipio está archivado. Los datos se conservan solo
                para consulta histórica.
              </p>
            ) : null}

            <dl className="municipal-hub-metrics">
              <div>
                <dt>Población</dt>
                <dd>
                  {formatInteger(municipality.population)}
                  <span>habitantes</span>
                </dd>
              </div>
              <div>
                <dt>Superficie</dt>
                <dd>{formatDecimal(municipality.surface_km2, "km²")}</dd>
              </div>
              <div>
                <dt>Densidad</dt>
                <dd>{formatDecimal(municipality.density, "hab./km²")}</dd>
              </div>
              <div>
                <dt>Código postal</dt>
                <dd>{municipality.postal_codes ?? "No consta"}</dd>
              </div>
            </dl>

            <div className="municipal-hub-columns">
              <section
                aria-labelledby="municipal-identity-heading"
                className="municipal-hub-card"
              >
                <header className="municipal-hub-card-header">
                  <h2 id="municipal-identity-heading">Identidad municipal</h2>
                  <span
                    className={`municipal-hub-status municipal-hub-status--${municipality.status}`}
                  >
                    {municipality.status === "active" ? "Activo" : "Archivado"}
                  </span>
                </header>
                <dl className="municipal-hub-details">
                  <div>
                    <dt>Código INE</dt>
                    <dd>{municipality.ine_code ?? "No consta"}</dd>
                  </div>
                  <div>
                    <dt>Tipo</dt>
                    <dd>
                      {MUNICIPALITY_TYPE_LABELS[municipality.municipality_type]}
                    </dd>
                  </div>
                  <div>
                    <dt>Perfil territorial</dt>
                    <dd>
                      {RURAL_URBAN_PROFILE_LABELS[
                        municipality.rural_urban_profile
                      ]}
                    </dd>
                  </div>
                  <div>
                    <dt>Provincia</dt>
                    <dd>{municipality.province}</dd>
                  </div>
                  <div>
                    <dt>Comunidad autónoma</dt>
                    <dd>{municipality.autonomous_community}</dd>
                  </div>
                  <div>
                    <dt>Organización seleccionada</dt>
                    <dd>{selectedContext.organization.name}</dd>
                  </div>
                </dl>
                <p className="municipal-hub-updated">
                  Última actualización: {formatUpdatedAt(municipality.updated_at)}
                </p>
              </section>

              <section
                aria-labelledby="municipal-profile-heading"
                className="municipal-hub-card"
              >
                <header className="municipal-hub-card-header">
                  <h2 id="municipal-profile-heading">Perfiles y notas</h2>
                </header>
                <dl className="municipal-hub-notes">
                  <div>
                    <dt>Perfil económico</dt>
                    <dd>
                      {municipality.economic_profile ?? "No consta información."}
                    </dd>
                  </div>
                  <div>
                    <dt>Perfil turístico</dt>
                    <dd>
                      {municipality.tourism_profile ?? "No consta información."}
                    </dd>
                  </div>
                  <div>
                    <dt>Notas geográficas</dt>
                    <dd>
                      {municipality.geographic_notes ?? "No consta información."}
                    </dd>
                  </div>
                  <div>
                    <dt>Notas administrativas</dt>
                    <dd>
                      {municipality.administrative_notes ??
                        "No consta información."}
                    </dd>
                  </div>
                </dl>
              </section>
            </div>

            {moduleLinks.length > 0 ? (
              <section
                aria-labelledby="municipal-modules-heading"
                className="municipal-hub-modules"
              >
                <div>
                  <p className="eyebrow">Accesos disponibles</p>
                  <h2 id="municipal-modules-heading">Módulos reales</h2>
                </div>
                <div className="municipal-hub-module-grid">
                  {moduleLinks.map((module) => (
                    <Link href={module.href} key={module.href}>
                      <span>{module.label}</span>
                      <small>{module.description}</small>
                      <strong aria-hidden="true">→</strong>
                    </Link>
                  ))}
                </div>
              </section>
            ) : null}
          </>
        ) : null}
      </div>
    </section>
  );
}
