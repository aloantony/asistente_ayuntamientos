"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  useCallback,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { userHasPermission, type User } from "../components/types";
import { fetchRequirementsTotal } from "../lib/fetchers";
import {
  consumePendingLoginRedirect,
  shouldShowAdminPanel,
  shouldShowProjectsPanel,
  shouldShowRequirementsPanel,
  useSession,
} from "../lib/session";

const ONBOARDING_STORAGE_PREFIX = "anacleto:onboarding:v1";

type OnboardingStepId = "theme" | "assistant" | "account";

type OnboardingStep = {
  id: OnboardingStepId;
  eyebrow: string;
  title: string;
  body: string;
};

function getMunicipalBrandName(user: User) {
  const organization = user.organizations?.[0];
  const municipalityName = organization?.municipality?.name?.trim();

  if (municipalityName) {
    return municipalityName;
  }

  const organizationName = organization?.name?.trim();

  if (organizationName) {
    return organizationName.replace(/^Ayuntamiento\s+de\s+/i, "");
  }

  return "Anacleto";
}

// Iconos del menú lateral (trazo fino, coherentes con el resto del shell).
type NavIconName =
  | "home"
  | "needs"
  | "map"
  | "projects"
  | "anacleto"
  | "admin"
  | "account";

function NavIcon({ name }: { name: NavIconName }) {
  const common = {
    "aria-hidden": true,
    fill: "none",
    height: 17,
    stroke: "currentColor",
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    strokeWidth: 1.6,
    viewBox: "0 0 24 24",
    width: 17,
  };
  switch (name) {
    case "home":
      return (
        <svg {...common}>
          <rect x="3" y="3" width="7" height="7" rx="1.5" />
          <rect x="14" y="3" width="7" height="7" rx="1.5" />
          <rect x="3" y="14" width="7" height="7" rx="1.5" />
          <rect x="14" y="14" width="7" height="7" rx="1.5" />
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
    case "projects":
      return (
        <svg {...common}>
          <path d="M4 5h5l2 2.5h9A1.5 1.5 0 0 1 21 9v9.5A1.5 1.5 0 0 1 19.5 20h-15A1.5 1.5 0 0 1 3 18.5v-12A1.5 1.5 0 0 1 4 5Z" />
        </svg>
      );
    case "map":
      return (
        <svg {...common}>
          <path d="M9 18 3.5 21V6L9 3l6 3 5.5-3v15L15 21l-6-3Z" />
          <path d="M9 3v15" />
          <path d="M15 6v15" />
        </svg>
      );
    case "anacleto":
      return (
        <svg aria-hidden="true" height="17" viewBox="0 0 24 24" width="17">
          <use href="/icons/assistant-symbols.svg#icon-assistant-mark" />
        </svg>
      );
    case "admin":
      return (
        <svg {...common}>
          <path d="M4 8h10M18 8h2M4 16h2M10 16h10" />
          <circle cx="16" cy="8" r="2" />
          <circle cx="8" cy="16" r="2" />
        </svg>
      );
    case "account":
      return (
        <svg {...common}>
          <circle cx="12" cy="8" r="3.5" />
          <path d="M5 20c0-3.3 3.1-6 7-6s7 2.7 7 6" />
        </svg>
      );
  }
}

type NavItem = {
  href: string;
  label: string;
  icon: NavIconName;
  // "Inicio" vive en "/", prefijo de todo lo demás: solo se marca activo con
  // coincidencia exacta; el resto usa startsWith.
  exact?: boolean;
  // Conteo real opcional (null mientras carga / si falla → sin badge).
  badge?: number | null;
  beta?: boolean;
};

type NavGroup = { label: string; items: NavItem[] };

// El tema (claro/oscuro) lo aplica el script anti-parpadeo del layout raíz
// añadiendo la clase .dark a <html>; aquí sólo leemos ese estado tras montar
// (para no romper la hidratación) y lo alternamos, persistiéndolo en
// localStorage. Ver ADR-012 sobre el resto de preferencias locales.
function useDarkMode() {
  const [dark, setDark] = useState(false);

  useEffect(() => {
    setDark(document.documentElement.classList.contains("dark"));
  }, []);

  const toggle = useCallback(() => {
    setDark((prev) => {
      const next = !prev;
      document.documentElement.classList.toggle("dark", next);
      try {
        localStorage.setItem("theme", next ? "dark" : "light");
      } catch {
        // Modo privado / almacenamiento bloqueado: el tema sólo dura la sesión.
      }
      return next;
    });
  }, []);

  return { dark, toggle };
}

function getUserInitials(fullName: string) {
  const initials = fullName
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("");

  return initials || "U";
}

function getOnboardingStorageKey(userId: number) {
  return `${ONBOARDING_STORAGE_PREFIX}:${userId}`;
}

export default function AppLayout({
  children,
}: Readonly<{
  children: ReactNode;
}>) {
  const router = useRouter();
  const pathname = usePathname();
  const { user, isLoadingSession, logout } = useSession();
  const [isMenuOpen, setIsMenuOpen] = useState(false);
  const [requirementsTotal, setRequirementsTotal] = useState<number | null>(
    null,
  );
  const [showOnboarding, setShowOnboarding] = useState(false);
  const [activeOnboardingIndex, setActiveOnboardingIndex] = useState(0);
  const { dark, toggle: toggleTheme } = useDarkMode();

  // Conteo real de necesidades para el badge del menú. El layout (app) no se
  // desmonta al navegar entre secciones, así que se pide una sola vez por
  // sesión. Si falla (o no hay permiso) simplemente no se muestra el badge.
  useEffect(() => {
    if (!user || !shouldShowRequirementsPanel(user)) {
      return;
    }
    let isActive = true;
    fetchRequirementsTotal()
      .then((total) => {
        if (isActive) {
          setRequirementsTotal(total);
        }
      })
      .catch(() => undefined);
    return () => {
      isActive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id]);

  useEffect(() => {
    if (!isLoadingSession && !user) {
      if (consumePendingLoginRedirect()) {
        // logout()/handleRequestError ya han navegado a /login con sus
        // propios parámetros (expired, next); no se sobrescribe esa URL.
        return;
      }

      // Conserva el destino (ruta + filtros/selección) para volver tras
      // iniciar sesión; "/" y /login no aportan nada como retorno.
      const target = window.location.pathname + window.location.search;
      const canReturn =
        target.startsWith("/") &&
        !target.startsWith("//") &&
        target !== "/" &&
        !target.startsWith("/login");
      router.replace(
        canReturn ? `/login?next=${encodeURIComponent(target)}` : "/login",
      );
    }
  }, [isLoadingSession, user, router]);

  useEffect(() => {
    if (!user) {
      setShowOnboarding(false);
      return;
    }

    try {
      setShowOnboarding(
        window.localStorage.getItem(getOnboardingStorageKey(user.id)) !== "seen",
      );
      setActiveOnboardingIndex(0);
    } catch {
      // Si el navegador bloquea localStorage, mostramos la ayuda sólo en esta
      // sesión; nunca debe impedir usar la aplicación.
      setShowOnboarding(true);
      setActiveOnboardingIndex(0);
    }
  }, [user]);

  // Close the mobile menu after navigating to another section.
  useEffect(() => {
    setIsMenuOpen(false);
  }, [pathname]);

  // The mobile menu also closes with Escape, as a standard dropdown.
  useEffect(() => {
    if (!isMenuOpen) {
      return;
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setIsMenuOpen(false);
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [isMenuOpen]);

  useEffect(() => {
    if (!showOnboarding) {
      return;
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        dismissOnboarding();
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showOnboarding]);

  if (isLoadingSession) {
    return (
      <main className="page">
        <section className="panel">
          <p className="eyebrow">Plataforma privada municipal</p>
          <h1>Comprobando sesión</h1>
          <p className="muted">Validando tus credenciales guardadas.</p>
        </section>
      </main>
    );
  }

  if (!user) {
    // The effect above redirects to the login page.
    return null;
  }

  const canUseAssistant = userHasPermission(user, "assistant.use");
  const canViewProjects = shouldShowProjectsPanel(user);
  const canViewMap =
    userHasPermission(user, "map.view") || userHasPermission(user, "map.manage");
  const brandName = getMunicipalBrandName(user);
  const userInitials = getUserInitials(user.full_name);

  const onboardingSteps: OnboardingStep[] = [
    {
      id: "theme",
      eyebrow: "Tema",
      title: "Claro u oscuro",
      body: "Cambia el modo visual desde este icono. Lo recordaremos en este navegador.",
    },
    ...(canUseAssistant
      ? [
          {
            id: "assistant" as const,
            eyebrow: "Anacleto",
            title: "Asistente municipal",
            body: "Abre el chat para consultar información visible, organizar trabajo o preparar borradores supervisados.",
          },
        ]
      : []),
    {
      id: "account",
      eyebrow: "Cuenta",
      title: "Tu perfil",
      body: "Revisa aquí tus datos, organización y permisos dentro del ayuntamiento.",
    },
  ];

  const safeOnboardingIndex = Math.min(
    activeOnboardingIndex,
    onboardingSteps.length - 1,
  );
  const activeOnboardingStep = onboardingSteps[safeOnboardingIndex];

  const navGroups: NavGroup[] = [
    {
      label: "Trabajo",
      items: [
        { href: "/", label: "Inicio", icon: "home", exact: true },
        ...(shouldShowRequirementsPanel(user)
          ? [
              {
                href: "/requisitos",
                label: "Necesidades",
                icon: "needs" as const,
                badge: requirementsTotal,
              },
            ]
          : []),
        ...(canViewMap
          ? [{ href: "/mapa", label: "Mapa", icon: "map" as const }]
          : []),
        ...(canViewProjects
          ? [{ href: "/proyectos", label: "Proyectos", icon: "projects" as const }]
          : []),
      ],
    },
    ...(canUseAssistant
      ? [
          {
            label: "Inteligencia",
            items: [
              {
                href: "/asistente",
                label: "Anacleto",
                icon: "anacleto" as const,
                beta: true,
              },
            ],
          },
        ]
      : []),
    {
      label: "Gestión",
      items: [
        ...(shouldShowAdminPanel(user)
          ? [
              {
                href: "/admin",
                label: "Administración",
                icon: "admin" as const,
              },
            ]
          : []),
        { href: "/cuenta", label: "Mi cuenta", icon: "account" },
      ],
    },
  ];

  function isActive(item: NavItem) {
    return item.exact ? pathname === item.href : pathname.startsWith(item.href);
  }

  function dismissOnboarding() {
    setShowOnboarding(false);
    setActiveOnboardingIndex(0);
    if (!user) {
      return;
    }
    try {
      window.localStorage.setItem(getOnboardingStorageKey(user.id), "seen");
    } catch {
      // El cierre visual basta si no se puede persistir la preferencia.
    }
  }

  function handleThemeToggle() {
    toggleTheme();
  }

  function goToPreviousOnboardingStep() {
    setActiveOnboardingIndex((index) => Math.max(0, index - 1));
  }

  function goToNextOnboardingStep() {
    setActiveOnboardingIndex((index) =>
      Math.min(onboardingSteps.length - 1, index + 1),
    );
  }

  const themeToggleLabel = dark ? "Cambiar a modo claro" : "Cambiar a modo oscuro";

  const contentClassName =
    pathname === "/asistente"
      ? "app-content app-content-assistant"
      : "app-content app-content-wide";

  return (
    <div className="app-shell">
      <aside className="app-sidebar">
        <div className="app-brand">
          <span className="app-brand-star" aria-hidden="true">
            <img alt="" src="/brand/logo-principal.svg" />
          </span>
          <span className="app-brand-name" title={brandName}>
            {brandName}
          </span>
        </div>
        <button
          aria-expanded={isMenuOpen}
          className="menu-toggle"
          onClick={() => setIsMenuOpen((open) => !open)}
          type="button"
        >
          Menú
        </button>
        <nav className={isMenuOpen ? "app-nav app-nav--open" : "app-nav"}>
          {navGroups
            .filter((group) => group.items.length > 0)
            .map((group) => (
              <div className="app-nav-group" key={group.label}>
                <span className="app-nav-section">{group.label}</span>
                {group.items.map((item) => (
                  <Link
                    className={`app-nav-link${isActive(item) ? " active" : ""}`}
                    href={item.href}
                    key={item.href}
                    // Cierra también al pulsar la sección ya activa, donde el
                    // pathname no cambia y el efecto de navegación no se dispara.
                    onClick={() => setIsMenuOpen(false)}
                  >
                    <NavIcon name={item.icon} />
                    <span className="app-nav-label">{item.label}</span>
                    {item.beta ? (
                      <span className="app-nav-beta">BETA</span>
                    ) : null}
                    {typeof item.badge === "number" ? (
                      <span className="app-nav-badge">{item.badge}</span>
                    ) : null}
                  </Link>
                ))}
              </div>
            ))}
        </nav>
        <div className="app-session">
          <button
            aria-pressed={dark}
            aria-label={themeToggleLabel}
            className="app-theme-toggle"
            onClick={handleThemeToggle}
            title={themeToggleLabel}
            type="button"
          >
            {dark ? (
              <svg
                fill="none"
                height="17"
                stroke="currentColor"
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth="1.6"
                viewBox="0 0 24 24"
                width="17"
              >
                <circle cx="12" cy="12" r="4" />
                <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
              </svg>
            ) : (
              <svg
                fill="none"
                height="17"
                stroke="currentColor"
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth="1.6"
                viewBox="0 0 24 24"
                width="17"
              >
                <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
              </svg>
            )}
            <span>{dark ? "Modo claro" : "Modo oscuro"}</span>
          </button>
          <div className="app-session-user">
            <p className="muted">{user.full_name}</p>
            {user.organizations && user.organizations.length > 0 ? (
              <p className="app-session-org">{user.organizations[0].name}</p>
            ) : null}
          </div>
          <button
            className="secondary-button"
            onClick={() => logout()}
            type="button"
          >
            Cerrar sesión
          </button>
        </div>
      </aside>
      <div className="app-main">
        <header
          className={
            showOnboarding
              ? `app-topbar onboarding-active onboarding-step-${activeOnboardingStep.id}`
              : "app-topbar"
          }
        >
          <div className="app-topbar-actions">
            <button
              aria-describedby={
                showOnboarding && activeOnboardingStep.id === "theme"
                  ? "app-onboarding-popover"
                  : undefined
              }
              aria-label={themeToggleLabel}
              aria-pressed={dark}
              className="app-topbar-theme"
              onClick={handleThemeToggle}
              title={themeToggleLabel}
              type="button"
            >
              {dark ? (
                <svg
                  aria-hidden="true"
                  fill="none"
                  height="17"
                  stroke="currentColor"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth="1.6"
                  viewBox="0 0 24 24"
                  width="17"
                >
                  <circle cx="12" cy="12" r="4" />
                  <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
                </svg>
              ) : (
                <svg
                  aria-hidden="true"
                  fill="none"
                  height="17"
                  stroke="currentColor"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth="1.6"
                  viewBox="0 0 24 24"
                  width="17"
                >
                  <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
                </svg>
              )}
            </button>
            {canUseAssistant ? (
              <button
                aria-describedby={
                  showOnboarding && activeOnboardingStep.id === "assistant"
                    ? "app-onboarding-popover"
                    : undefined
                }
                aria-label="Abrir asistente"
                className="app-topbar-anacleto"
                onClick={() => router.push("/asistente")}
                title="Abrir asistente"
                type="button"
              >
                <svg aria-hidden="true" height="15" viewBox="0 0 24 24" width="15">
                  <use href="/icons/assistant-symbols.svg#icon-assistant-mark" />
                </svg>
              </button>
            ) : null}
            <button
              aria-describedby={
                showOnboarding && activeOnboardingStep.id === "account"
                  ? "app-onboarding-popover"
                  : undefined
              }
              aria-label="Abrir mi cuenta"
              className="app-topbar-user"
              onClick={() => router.push("/cuenta")}
              title={user.full_name}
              type="button"
            >
              {userInitials}
            </button>
          </div>
          {showOnboarding ? (
            <div
              aria-labelledby="app-onboarding-title"
              className="app-onboarding-layer"
              id="app-onboarding-popover"
              role="dialog"
            >
              <button
                aria-label="Cerrar ayuda inicial"
                className="app-onboarding-scrim"
                onClick={dismissOnboarding}
                type="button"
              />
              <section
                className={`app-onboarding-note app-onboarding-note-${activeOnboardingStep.id}`}
              >
                <p className="eyebrow">{activeOnboardingStep.eyebrow}</p>
                <h2 id="app-onboarding-title">{activeOnboardingStep.title}</h2>
                <p>{activeOnboardingStep.body}</p>
                {activeOnboardingStep.id === "theme" ? (
                  <button className="accent-button" onClick={handleThemeToggle} type="button">
                    Probar ahora
                  </button>
                ) : null}
                <div className="app-onboarding-controls" aria-label="Pasos de la ayuda inicial">
                  <button
                    className="secondary-button"
                    disabled={safeOnboardingIndex === 0}
                    onClick={goToPreviousOnboardingStep}
                    type="button"
                  >
                    Anterior
                  </button>
                  <span aria-live="polite">
                    {safeOnboardingIndex + 1}/{onboardingSteps.length}
                  </span>
                  <button
                    className="secondary-button"
                    disabled={safeOnboardingIndex === onboardingSteps.length - 1}
                    onClick={goToNextOnboardingStep}
                    type="button"
                  >
                    Siguiente
                  </button>
                </div>
                <button className="app-onboarding-dismiss" onClick={dismissOnboarding} type="button">
                  Cerrar ayuda
                </button>
              </section>
            </div>
          ) : null}
        </header>
        <main className={contentClassName}>
          {children}
        </main>
      </div>
    </div>
  );
}
