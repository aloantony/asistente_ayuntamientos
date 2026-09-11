"use client";

import {
  Check,
  ChevronDown,
  ExternalLink,
  FileText,
  GripVertical,
  Plus,
  Search,
  Sparkles,
} from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  fetchOrdinanceCatalog,
  fetchOrdinanceDetail,
} from "../lib/ordinances";
import {
  formatLegalDate,
  safeExternalUrl,
} from "../lib/ordinance-format";
import { useSession } from "../lib/session";
import {
  formatOrdinanceStatus,
  userHasPermission,
  type MunicipalitySummary,
  type Ordinance,
  type OrdinanceLegalChunk,
} from "./types";
import styles from "./OrdinanceCatalog.module.css";

export const ORDINANCE_CATEGORIES = [
  { id: "urban", label: "Urbanística y planeamiento" },
  { id: "municipal", label: "Ordenanzas municipales" },
  { id: "licenses", label: "Licencias y trámites" },
  { id: "traffic", label: "Tráfico y movilidad" },
  { id: "tax", label: "Hacienda municipal" },
  { id: "environment", label: "Medio ambiente y servicios" },
  { id: "staff", label: "Personal y administración" },
  { id: "other", label: "Otras normas" },
] as const;

export type OrdinanceCategoryId = (typeof ORDINANCE_CATEGORIES)[number]["id"];

function normalize(value: string | null | undefined) {
  return (value ?? "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLocaleLowerCase("es");
}

export function classifyOrdinance(ordinance: Ordinance): OrdinanceCategoryId {
  const title = normalize(ordinance.title);
  if (/urban|planeamiento|suelo|edific|obra|plan parcial/.test(title)) {
    return "urban";
  }
  if (/licencia|tramite|actividad|apertura/.test(title)) {
    return "licenses";
  }
  if (/trafico|movilidad|vehicul|estacionamiento|circulacion/.test(title)) {
    return "traffic";
  }
  if (/residuo|basura|agua|saneamiento|ruido|limpieza/.test(title)) {
    return "environment";
  }
  if (/hacienda|fiscal|tribut|impuesto|tasa|ibi|ivtm|plusvalia/.test(title)) {
    return "tax";
  }

  const text = normalize(
    [
      ordinance.topic,
      ordinance.subtopic,
      ordinance.summary,
      ordinance.ordinance_type,
    ]
      .filter(Boolean)
      .join(" "),
  );

  if (/urban|planeamiento|suelo|edific|obra|plan parcial/.test(text)) {
    return "urban";
  }
  if (/licencia|tramite|actividad|apertura/.test(text)) {
    return "licenses";
  }
  if (/trafico|movilidad|vehicul|estacionamiento|circulacion/.test(text)) {
    return "traffic";
  }
  if (
    /medio ambiente|residuo|basura|agua|saneamiento|ruido|limpieza|servicio/.test(
      text,
    )
  ) {
    return "environment";
  }
  if (/hacienda|fiscal|tribut|impuesto|tasa|ibi|ivtm|plusvalia/.test(text)) {
    return "tax";
  }
  if (/personal|empleo|administracion|funcionario|plantilla/.test(text)) {
    return "staff";
  }
  if (/ordenanza|reglamento|bando/.test(text)) {
    return "municipal";
  }
  return "other";
}

export function compactOrdinanceTitle(title: string) {
  const [base] = title.split(/\s+-\s+/);
  return base.trim() || title;
}

function uniqueMunicipalities(userOrganizations: OrdinanceWorkspaceUser["organizations"]) {
  const byId = new Map<number, MunicipalitySummary>();
  userOrganizations?.forEach((organization) => {
    if (organization.status !== "archived" && organization.municipality) {
      byId.set(organization.municipality.id, organization.municipality);
    }
  });
  return [...byId.values()].sort((left, right) =>
    left.name.localeCompare(right.name, "es"),
  );
}

type OrdinanceWorkspaceUser = NonNullable<ReturnType<typeof useSession>["user"]>;

function formattedUpdate(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "No consta";
  }
  return new Intl.DateTimeFormat("es-ES", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  }).format(date);
}

function DetailPane({
  detail,
  error,
  isLoading,
}: {
  detail: { ordinance: Ordinance; chunks: OrdinanceLegalChunk[] } | null;
  error: string;
  isLoading: boolean;
}) {
  if (isLoading) {
    return (
      <div className={styles.detailState} role="status">
        <span className={styles.spinner} />
        <p>Cargando la ficha normativa…</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.detailState} role="alert">
        <FileText aria-hidden="true" />
        <strong>No se pudo abrir la norma</strong>
        <p>{error}</p>
      </div>
    );
  }

  if (!detail) {
    return (
      <div className={styles.detailState}>
        <FileText aria-hidden="true" />
        <strong>Selecciona una norma</strong>
        <p>Su ficha y el contenido revisado aparecerán aquí.</p>
      </div>
    );
  }

  const { ordinance, chunks } = detail;
  const sourceUrl = safeExternalUrl(ordinance.source_url);
  const preview =
    ordinance.text_content?.trim() ||
    chunks
      .map((chunk) =>
        [chunk.citation ?? chunk.heading, chunk.text].filter(Boolean).join("\n"),
      )
      .join("\n\n");

  const statusOptions = [
    { key: "active", label: "Vigente", selected: ordinance.status === "active" },
    {
      key: "repealed",
      label: "Derogada",
      selected: ordinance.status === "repealed",
    },
    {
      key: "review",
      label: "En revisión",
      selected:
        ordinance.status === "unknown" ||
        ordinance.curation_status !== "approved",
    },
  ];

  return (
    <>
      <header className={styles.detailToolbar}>
        <div>
          <span>{ordinance.municipality.name}</span>
          <h2>{ordinance.title}</h2>
        </div>
        {sourceUrl ? (
          <a href={sourceUrl} rel="noreferrer" target="_blank">
            <ExternalLink aria-hidden="true" />
            Abrir
          </a>
        ) : null}
      </header>

      <div className={styles.detailScroll}>
        <section className={styles.fieldBlock}>
          <h3>Descripción</h3>
          <p>{ordinance.summary || "Sin descripción breve registrada."}</p>
        </section>

        <section className={styles.statusBlock}>
          <h3>Estado</h3>
          <div className={styles.statusOptions}>
            {statusOptions.map((option) => (
              <span
                className={option.selected ? styles.statusSelected : undefined}
                key={option.key}
              >
                {option.label}
              </span>
            ))}
            {!statusOptions.some((option) => option.selected) ? (
              <span className={styles.statusSelected}>
                {formatOrdinanceStatus(ordinance.status)}
              </span>
            ) : null}
          </div>
        </section>

        <dl className={styles.metadataGrid}>
          <div>
            <dt>Fecha de aprobación</dt>
            <dd>{formatLegalDate(ordinance.approval_date)}</dd>
          </div>
          <div>
            <dt>Publicación</dt>
            <dd>{formatLegalDate(ordinance.publication_date)}</dd>
          </div>
          <div>
            <dt>Última actualización</dt>
            <dd>{formattedUpdate(ordinance.updated_at)}</dd>
          </div>
          <div>
            <dt>Boletín</dt>
            <dd>
              {[ordinance.official_bulletin, ordinance.bulletin_number]
                .filter(Boolean)
                .join(" · ") || "No consta"}
            </dd>
          </div>
        </dl>

        <section className={styles.linkBlock}>
          <h3>Enlace externo (BOP/BOCyL)</h3>
          {sourceUrl ? (
            <a href={sourceUrl} rel="noreferrer" target="_blank">
              <span>{sourceUrl}</span>
              <ExternalLink aria-hidden="true" />
            </a>
          ) : (
            <p>Sin enlace externo registrado.</p>
          )}
        </section>

        <section className={styles.attachmentsBlock}>
          <h3>Adjuntos</h3>
          {ordinance.document ? (
            <div>
              <FileText aria-hidden="true" />
              <span>{ordinance.document.original_filename}</span>
            </div>
          ) : (
            <p>Sin adjuntos.</p>
          )}
        </section>

        <section className={styles.preview}>
          <div className={styles.previewHeading}>
            <div>
              <span>Contenido normativo</span>
              <strong>Vista previa revisada</strong>
            </div>
            <small>
              {chunks.length} {chunks.length === 1 ? "fragmento" : "fragmentos"}
            </small>
          </div>
          {preview ? (
            <pre>{preview}</pre>
          ) : (
            <div className={styles.previewEmpty}>
              <FileText aria-hidden="true" />
              <p>Esta ficha todavía no tiene texto revisado para previsualizar.</p>
            </div>
          )}
        </section>
      </div>
    </>
  );
}

export function OrdinanceCatalog({
  onOpenAdvancedSearch,
}: {
  onOpenAdvancedSearch: () => void;
}) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const paramsSignature = searchParams.toString();
  const { user, handleRequestError } = useSession();
  const handleRequestErrorRef = useRef(handleRequestError);
  const [ordinances, setOrdinances] = useState<Ordinance[]>([]);
  const [total, setTotal] = useState(0);
  const [query, setQuery] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [catalogError, setCatalogError] = useState("");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<{
    ordinance: Ordinance;
    chunks: OrdinanceLegalChunk[];
  } | null>(null);
  const [isLoadingDetail, setIsLoadingDetail] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [openCategories, setOpenCategories] = useState<Set<OrdinanceCategoryId>>(
    () => new Set(),
  );

  useEffect(() => {
    handleRequestErrorRef.current = handleRequestError;
  }, [handleRequestError]);

  const municipalities = useMemo(
    () => uniqueMunicipalities(user?.organizations),
    [user?.organizations],
  );
  const requestedMunicipalityId = Number.parseInt(
    searchParams.get("municipality_id") ?? "",
    10,
  );
  const municipality =
    municipalities.find((item) => item.id === requestedMunicipalityId) ??
    municipalities[0] ??
    null;
  const requestedOrdinanceId = Number.parseInt(
    searchParams.get("id") ?? "",
    10,
  );
  const canManage = Boolean(
    user &&
      (user.is_superuser ||
        [
          "ordinances.create",
          "ordinances.edit",
          "ordinances.import",
          "ordinances.manage",
        ].some((permission) => userHasPermission(user, permission))),
  );

  useEffect(() => {
    if (!user) {
      return;
    }
    const controller = new AbortController();
    setIsLoading(true);
    setCatalogError("");

    fetchOrdinanceCatalog(municipality?.id, controller.signal)
      .then((collection) => {
        if (controller.signal.aborted) {
          return;
        }
        setOrdinances(collection.items);
        setTotal(collection.total);
        setSelectedId((current) =>
          current && collection.items.some((item) => item.id === current)
            ? current
            : (collection.items[0]?.id ?? null),
        );
      })
      .catch((error) => {
        if (!controller.signal.aborted) {
          handleRequestErrorRef.current(
            error,
            setCatalogError,
            "No se pudo cargar el catálogo normativo.",
          );
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setIsLoading(false);
        }
      });

    return () => controller.abort();
  }, [municipality?.id, user]);

  useEffect(() => {
    if (
      Number.isInteger(requestedOrdinanceId) &&
      ordinances.some((ordinance) => ordinance.id === requestedOrdinanceId)
    ) {
      setSelectedId(requestedOrdinanceId);
    }
  }, [ordinances, requestedOrdinanceId]);

  useEffect(() => {
    const selected = ordinances.find((ordinance) => ordinance.id === selectedId);
    if (!selected) {
      return;
    }
    const category = classifyOrdinance(selected);
    setOpenCategories((current) => {
      if (current.has(category)) {
        return current;
      }
      const next = new Set(current);
      next.add(category);
      return next;
    });
  }, [ordinances, selectedId]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      setDetailError("");
      return;
    }
    const controller = new AbortController();
    setIsLoadingDetail(true);
    setDetailError("");
    fetchOrdinanceDetail(selectedId, { signal: controller.signal })
      .then((loaded) => {
        if (!controller.signal.aborted) {
          setDetail(loaded);
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted) {
          handleRequestErrorRef.current(
            error,
            setDetailError,
            "No se pudo cargar la ficha normativa.",
          );
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setIsLoadingDetail(false);
        }
      });
    return () => controller.abort();
  }, [selectedId]);

  const filtered = useMemo(() => {
    const needle = normalize(query.trim());
    if (!needle) {
      return ordinances;
    }
    return ordinances.filter((ordinance) =>
      normalize(
        [
          ordinance.title,
          ordinance.topic,
          ordinance.subtopic,
          ordinance.summary,
          ORDINANCE_CATEGORIES.find(
            (category) => category.id === classifyOrdinance(ordinance),
          )?.label,
        ]
          .filter(Boolean)
          .join(" "),
      ).includes(needle),
    );
  }, [ordinances, query]);

  const groups = useMemo(
    () =>
      ORDINANCE_CATEGORIES.map((category) => ({
        ...category,
        ordinances: filtered.filter(
          (ordinance) => classifyOrdinance(ordinance) === category.id,
        ),
      })),
    [filtered],
  );

  useEffect(() => {
    if (
      filtered.length > 0 &&
      !filtered.some((ordinance) => ordinance.id === selectedId)
    ) {
      setSelectedId(filtered[0].id);
    }
  }, [filtered, selectedId]);

  function chooseOrdinance(ordinance: Ordinance) {
    setSelectedId(ordinance.id);
    const next = new URLSearchParams(paramsSignature);
    next.set("id", String(ordinance.id));
    if (municipality) {
      next.set("municipality_id", String(municipality.id));
    }
    router.replace(`/ordenanzas?${next.toString()}`, { scroll: false });
  }

  function chooseMunicipality(municipalityId: number) {
    const next = new URLSearchParams(paramsSignature);
    next.set("municipality_id", String(municipalityId));
    next.delete("id");
    router.replace(`/ordenanzas?${next.toString()}`, { scroll: false });
  }

  function toggleCategory(categoryId: OrdinanceCategoryId) {
    setOpenCategories((current) => {
      const next = new Set(current);
      if (next.has(categoryId)) {
        next.delete(categoryId);
      } else {
        next.add(categoryId);
      }
      return next;
    });
  }

  if (!user) {
    return null;
  }

  return (
    <section aria-labelledby="normativa-heading" className={styles.workspace}>
      <aside className={styles.catalog}>
        <header className={styles.catalogHeader}>
          <div>
            <h1 id="normativa-heading">Normativa</h1>
            <span>{total}</span>
          </div>
          {canManage ? (
            <button
              aria-label="Añadir normativa"
              onClick={() => router.push("/admin/ordenanzas")}
              type="button"
            >
              <Plus aria-hidden="true" />
            </button>
          ) : null}
        </header>

        {municipalities.length > 1 ? (
          <label className={styles.municipalityPicker}>
            <span>Municipio</span>
            <select
              onChange={(event) => chooseMunicipality(Number(event.target.value))}
              value={municipality?.id ?? ""}
            >
              {municipalities.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
        ) : municipality ? (
          <p className={styles.municipalityName}>{municipality.name}</p>
        ) : null}

        <label className={styles.catalogSearch}>
          <Search aria-hidden="true" />
          <input
            aria-label="Buscar normativa"
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Buscar por nombre, categoría o descripción"
            type="search"
            value={query}
          />
        </label>

        <div className={styles.categoryList}>
          {isLoading ? (
            <p className={styles.catalogMessage}>Cargando normativa…</p>
          ) : null}
          {!isLoading && catalogError ? (
            <p className={styles.catalogError} role="alert">
              {catalogError}
            </p>
          ) : null}
          {!isLoading &&
            !catalogError &&
            groups.map((group) => {
              const isOpen =
                query.trim().length > 0
                  ? group.ordinances.length > 0
                  : openCategories.has(group.id);
              return (
                <section className={styles.category} key={group.id}>
                  <button
                    aria-expanded={isOpen}
                    className={styles.categoryToggle}
                    onClick={() => toggleCategory(group.id)}
                    type="button"
                  >
                    <ChevronDown aria-hidden="true" />
                    <span>{group.label}</span>
                    <small>{group.ordinances.length}</small>
                  </button>
                  {isOpen ? (
                    <div className={styles.categoryContent}>
                      {group.ordinances.map((ordinance) => (
                        <button
                          aria-label={ordinance.title}
                          className={
                            selectedId === ordinance.id
                              ? styles.ordinanceSelected
                              : styles.ordinanceRow
                          }
                          key={ordinance.id}
                          onClick={() => chooseOrdinance(ordinance)}
                          type="button"
                        >
                          <GripVertical aria-hidden="true" />
                          <span className={styles.statusCheck}>
                            <Check aria-hidden="true" />
                          </span>
                          <span>
                            <strong>{compactOrdinanceTitle(ordinance.title)}</strong>
                            <small>
                              {formatLegalDate(
                                ordinance.publication_date ??
                                  ordinance.approval_date,
                              )}
                            </small>
                          </span>
                        </button>
                      ))}
                      {group.ordinances.length === 0 ? (
                        <p className={styles.emptyCategory}>Sin normas registradas.</p>
                      ) : null}
                      {canManage ? (
                        <button
                          className={styles.addDocuments}
                          onClick={() => router.push("/admin/ordenanzas")}
                          type="button"
                        >
                          <Plus aria-hidden="true" />
                          Añadir PDFs
                        </button>
                      ) : null}
                    </div>
                  ) : null}
                </section>
              );
            })}
          {!isLoading &&
          !catalogError &&
          query.trim() &&
          filtered.length === 0 ? (
            <p className={styles.catalogMessage}>
              No hay normas que coincidan con la búsqueda.
            </p>
          ) : null}
        </div>

        <button
          className={styles.advancedSearch}
          onClick={onOpenAdvancedSearch}
          type="button"
        >
          <Sparkles aria-hidden="true" />
          Búsqueda jurídica avanzada
        </button>
      </aside>

      <article className={styles.detail}>
        <DetailPane
          detail={detail}
          error={detailError}
          isLoading={isLoadingDetail}
        />
      </article>
    </section>
  );
}
