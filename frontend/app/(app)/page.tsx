"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import type { Project } from "../components/types";
import { formatProjectStatus, userHasPermission } from "../components/types";
import {
  fetchMunicipalitiesTotal,
  fetchOrdinancesTotal,
  fetchProjects,
  fetchRequirementsTotal,
} from "../lib/fetchers";
import {
  MUNICIPALITY_PERMISSIONS,
  ORDINANCE_PERMISSIONS,
} from "../lib/permissions";
import {
  hasAnyPermission,
  shouldShowRequirementsPanel,
  useSession,
} from "../lib/session";

// Saludo según la hora local del navegador del usuario.
function greetingForHour(hour: number) {
  if (hour >= 6 && hour < 13) {
    return "Buenos días";
  }
  if (hour >= 13 && hour < 21) {
    return "Buenas tardes";
  }
  return "Buenas noches";
}

function firstNameOf(fullName: string) {
  const trimmed = fullName.trim();
  if (!trimmed) {
    return "";
  }
  return trimmed.split(/\s+/)[0];
}

// Atajos frecuentes que se vuelcan tal cual en el asistente (?q=). Son tareas
// reales del producto, no respuestas de IA prefabricadas.
const ANACLETO_SUGGESTIONS = [
  "Ayúdame a redactar una necesidad nueva a partir de mis notas.",
  "Compara dos ordenanzas de municipios distintos.",
  "Localiza los documentos de un proyecto.",
];

// Iconos del panel: pequeños trazos coherentes con el resto del shell.
function StarIcon() {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      height="18"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.6"
      viewBox="0 0 24 24"
      width="18"
    >
      <path d="M12 3 L13.6 10.4 21 12 13.6 13.6 12 21 10.4 13.6 3 12 10.4 10.4 Z" />
    </svg>
  );
}

function ArrowIcon() {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      height="14"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="2"
      viewBox="0 0 24 24"
      width="14"
    >
      <path d="M5 12h14" />
      <path d="m13 6 6 6-6 6" />
    </svg>
  );
}

type StatIconName = "projects" | "needs" | "municipalities" | "ordinances";

function StatIcon({ name }: { name: StatIconName }) {
  const common = {
    "aria-hidden": true,
    fill: "none",
    height: 18,
    stroke: "currentColor",
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    strokeWidth: 1.6,
    viewBox: "0 0 24 24",
    width: 18,
  };
  switch (name) {
    case "projects":
      return (
        <svg {...common}>
          <path d="M4 5h5l2 2.5h9A1.5 1.5 0 0 1 21 9v9.5A1.5 1.5 0 0 1 19.5 20h-15A1.5 1.5 0 0 1 3 18.5v-12A1.5 1.5 0 0 1 4 5Z" />
        </svg>
      );
    case "needs":
      return (
        <svg {...common}>
          <path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2" />
          <rect x="9" y="3" width="6" height="4" rx="1" />
          <path d="m9 13 2 2 4-4" />
        </svg>
      );
    case "municipalities":
      return (
        <svg {...common}>
          <path d="M12 21s7-5.5 7-11a7 7 0 1 0-14 0c0 5.5 7 11 7 11Z" />
          <circle cx="12" cy="10" r="2.5" />
        </svg>
      );
    case "ordinances":
      return (
        <svg {...common}>
          <path d="M12 4v16" />
          <path d="m4 7 8-3 8 3" />
          <path d="M6 9 3 15a3 3 0 0 0 6 0L6 9Z" />
          <path d="M18 9l-3 6a3 3 0 0 0 6 0l-3-6Z" />
        </svg>
      );
  }
}

function ShieldIcon() {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      height="13"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.7"
      viewBox="0 0 24 24"
      width="13"
    >
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z" />
      <path d="m9 12 2 2 4-4" />
    </svg>
  );
}

type StatCard = {
  key: string;
  label: string;
  href: string;
  icon: StatIconName;
  // number = conteo real; null = no disponible (error). Mientras carga se
  // muestra un guion suave en lugar de una cifra inventada.
  value: number | null;
  // Segunda línea opcional con un dato REAL (nunca una tendencia inventada).
  hint?: string;
};

export default function HomePage() {
  const router = useRouter();
  const { user, handleRequestError } = useSession();

  const [askText, setAskText] = useState("");
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [requirementsTotal, setRequirementsTotal] = useState<number | null>(
    null,
  );
  const [municipalitiesTotal, setMunicipalitiesTotal] = useState<number | null>(
    null,
  );
  const [ordinancesTotal, setOrdinancesTotal] = useState<number | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState("");

  const canUseAssistant = Boolean(user && userHasPermission(user, "assistant.use"));
  const canSeeRequirements = Boolean(user && shouldShowRequirementsPanel(user));
  const canSeeMunicipalities = Boolean(
    user && hasAnyPermission(user, MUNICIPALITY_PERMISSIONS),
  );
  const canSeeOrdinances = Boolean(
    user && hasAnyPermission(user, ORDINANCE_PERMISSIONS),
  );

  useEffect(() => {
    if (!user) {
      return;
    }

    let isActive = true;
    setIsLoading(true);
    setError("");

    // Solo se piden los conteos que el usuario puede ver (cada endpoint exige
    // su permiso); así el panel nunca provoca un 403 ni inventa cifras.
    const tasks: Promise<unknown>[] = [
      fetchProjects()
        .then((data) => {
          if (isActive) {
            setProjects(data);
          }
        })
        .catch((loadError) => {
          if (isActive) {
            setProjects(null);
            handleRequestError(
              loadError,
              setError,
              "No se pudo cargar el panel de inicio.",
            );
          }
        }),
    ];

    if (canSeeRequirements) {
      tasks.push(
        fetchRequirementsTotal()
          .then((total) => isActive && setRequirementsTotal(total))
          .catch(() => isActive && setRequirementsTotal(null)),
      );
    }
    if (canSeeMunicipalities) {
      tasks.push(
        fetchMunicipalitiesTotal()
          .then((total) => isActive && setMunicipalitiesTotal(total))
          .catch(() => isActive && setMunicipalitiesTotal(null)),
      );
    }
    if (canSeeOrdinances) {
      tasks.push(
        fetchOrdinancesTotal()
          .then((total) => isActive && setOrdinancesTotal(total))
          .catch(() => isActive && setOrdinancesTotal(null)),
      );
    }

    void Promise.allSettled(tasks).then(() => {
      if (isActive) {
        setIsLoading(false);
      }
    });

    return () => {
      isActive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id]);

  const now = useMemo(() => new Date(), []);
  const dateLabel = useMemo(
    () =>
      now.toLocaleDateString("es-ES", {
        weekday: "long",
        day: "numeric",
        month: "long",
        year: "numeric",
      }),
    [now],
  );

  const recentProjects = useMemo(() => {
    if (!projects) {
      return [];
    }
    return [...projects]
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
      .slice(0, 5);
  }, [projects]);

  const stats = useMemo<StatCard[]>(() => {
    const activeProjects = projects
      ? projects.filter((project) => project.status === "active").length
      : null;
    const cards: StatCard[] = [
      {
        key: "projects",
        label: "Proyectos",
        href: "/proyectos",
        icon: "projects",
        value: projects ? projects.length : null,
        hint: activeProjects !== null ? `${activeProjects} activos` : undefined,
      },
    ];
    if (canSeeRequirements) {
      cards.push({
        key: "requirements",
        label: "Necesidades",
        href: "/requisitos",
        icon: "needs",
        value: requirementsTotal,
      });
    }
    if (canSeeMunicipalities) {
      cards.push({
        key: "municipalities",
        label: "Municipios",
        href: "/admin/municipios",
        icon: "municipalities",
        value: municipalitiesTotal,
      });
    }
    if (canSeeOrdinances) {
      cards.push({
        key: "ordinances",
        label: "Ordenanzas",
        href: "/admin/ordenanzas",
        icon: "ordinances",
        value: ordinancesTotal,
      });
    }
    return cards;
  }, [
    projects,
    canSeeRequirements,
    requirementsTotal,
    canSeeMunicipalities,
    municipalitiesTotal,
    canSeeOrdinances,
    ordinancesTotal,
  ]);

  if (!user) {
    // El layout (app) ya redirige al login cuando no hay sesión; este retorno
    // solo evita pintar el panel durante ese instante.
    return null;
  }

  function handleAsk(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const query = askText.trim();
    router.push(
      query ? `/asistente?q=${encodeURIComponent(query)}` : "/asistente",
    );
  }

  // Atajos reales: abren el asistente con la instrucción ya escrita. No son
  // salida inventada de la IA, sino accesos rápidos a tareas frecuentes.
  function askAnacleto(prompt: string) {
    router.push(`/asistente?q=${encodeURIComponent(prompt)}`);
  }

  const greeting = greetingForHour(now.getHours());
  const firstName = firstNameOf(user.full_name);

  return (
    <div className="dashboard">
      <header className="dashboard-head">
        <h1>
          {greeting}
          {firstName ? `, ${firstName}` : ""}
        </h1>
        <p className="dashboard-date">{dateLabel}</p>
      </header>

      {error ? <p className="error-message">{error}</p> : null}

      {canUseAssistant ? (
        <form className="dashboard-ask" onSubmit={handleAsk}>
          <span className="dashboard-ask-icon" aria-hidden="true">
            <StarIcon />
          </span>
          <input
            aria-label="Preguntar a Anacleto"
            onChange={(event) => setAskText(event.target.value)}
            placeholder="Pregúntale a Anacleto sobre tus proyectos, necesidades u ordenanzas…"
            value={askText}
          />
          <button className="accent-button" type="submit">
            Preguntar
            <ArrowIcon />
          </button>
        </form>
      ) : null}

      {stats.length > 0 ? (
        <div className="dashboard-stats">
          {stats.map((stat) => (
            <Link className="stat-card" href={stat.href} key={stat.key}>
              <div className="stat-card-top">
                <span className="stat-label">{stat.label}</span>
                <span className="stat-icon">
                  <StatIcon name={stat.icon} />
                </span>
              </div>
              <span className="stat-value">
                {isLoading ? "…" : (stat.value ?? "—")}
              </span>
              {stat.hint ? <span className="stat-hint">{stat.hint}</span> : null}
            </Link>
          ))}
        </div>
      ) : null}

      <div className="dashboard-cols">
        <section className="dashboard-recent">
          <div className="dashboard-section-head">
            <h3>Proyectos recientes</h3>
            <Link className="dashboard-see-all" href="/proyectos">
              Ver todos
            </Link>
          </div>
          {isLoading ? (
            <p className="small-muted">Cargando proyectos…</p>
          ) : recentProjects.length > 0 ? (
            <ul className="dashboard-recent-list">
              {recentProjects.map((project) => (
                <li key={project.id}>
                  <Link href="/proyectos">
                    <span className="dashboard-recent-name">
                      {project.name}
                    </span>
                    <span className="dashboard-recent-meta">
                      {project.organization.name} ·{" "}
                      {formatProjectStatus(project.status)}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          ) : (
            <p className="small-muted">
              Aún no hay proyectos. Cuando se creen, aparecerán aquí.
            </p>
          )}
        </section>

        <aside className="dashboard-anacleto">
          <div className="dashboard-anacleto-head">
            <StarIcon />
            <span>Anacleto</span>
          </div>
          <div className="dashboard-anacleto-body">
            {canUseAssistant ? (
              <>
                <p className="small-muted">Sugerencias para empezar:</p>
                <div className="dashboard-anacleto-suggestions">
                  {ANACLETO_SUGGESTIONS.map((suggestion) => (
                    <button
                      className="dashboard-suggestion"
                      key={suggestion}
                      onClick={() => askAnacleto(suggestion)}
                      type="button"
                    >
                      <span aria-hidden="true">→</span>
                      <span>{suggestion}</span>
                    </button>
                  ))}
                </div>
              </>
            ) : (
              <p className="field-helper">
                No tienes acceso al asistente en esta cuenta.
              </p>
            )}
            <p className="dashboard-anacleto-note">
              <ShieldIcon />
              Supervisado por humanos · datos pseudonimizados
            </p>
          </div>
        </aside>
      </div>
    </div>
  );
}
