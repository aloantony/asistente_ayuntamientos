"use client";

import { LogOut } from "lucide-react";
import { usePathname, useRouter } from "next/navigation";
import {
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { SidebarNavigation } from "../components/SidebarNavigation";
import { TopBar } from "../components/TopBar";
import { userHasPermission, type User } from "../components/types";
import { fetchRequirementsTotal } from "../lib/fetchers";
import {
  fetchTownHall,
  townHallShieldUrl,
  uploadTownHallShield,
} from "../lib/api";
import { activeTopNavSectionFor, shouldShowTopNav } from "../lib/topNav";
import {
  canEditTownHall,
  consumePendingLoginRedirect,
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
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const navigationRef = useRef<HTMLElement>(null);
  const [isMenuOpen, setIsMenuOpen] = useState(false);
  const [requirementsTotal, setRequirementsTotal] = useState<number | null>(
    null,
  );
  // Escudo del municipio para la barra superior. El diseño lo pone ahí, no en
  // el cuerpo del Ayuntamiento; `version` fuerza a saltarse la caché cuando se
  // sustituye, porque la URL no cambia.
  const [hasCrest, setHasCrest] = useState(false);
  const [crestVersion, setCrestVersion] = useState(0);
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

  // El perfil del ayuntamiento se pide una sola vez por sesión, como el badge
  // de necesidades: sólo interesa si hay escudo que pintar. Si falla, la barra
  // se queda con su marcador neutro.
  useEffect(() => {
    if (!user) {
      return;
    }
    let isActive = true;
    fetchTownHall()
      .then((townHall) => {
        if (isActive) {
          setHasCrest(townHall.profile.has_shield);
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

  const closeMobileMenu = useCallback((restoreFocus = false) => {
    setIsMenuOpen(false);
    if (restoreFocus) {
      window.requestAnimationFrame(() => menuButtonRef.current?.focus());
    }
  }, []);

  // Close the mobile menu after navigating to another section.
  useEffect(() => {
    setIsMenuOpen(false);
  }, [pathname]);

  // El drawer móvil coloca el foco en su primer control y lo devuelve al
  // disparador al cerrarse con Escape.
  useEffect(() => {
    if (!isMenuOpen) {
      return;
    }

    const isMobile = window.matchMedia("(max-width: 1900px)").matches;
    const previousBodyOverflow = document.body.style.overflow;
    const backgroundElements = isMobile
      ? Array.from(
          document.querySelectorAll<HTMLElement>(
            ".app-brand, .menu-toggle, .app-session, .app-main",
          ),
        )
      : [];
    const previouslyInertElements = new Set(
      backgroundElements.filter((element) => element.hasAttribute("inert")),
    );
    backgroundElements.forEach((element) => element.setAttribute("inert", ""));
    if (isMobile) {
      document.body.style.overflow = "hidden";
    }

    const focusFrame = window.requestAnimationFrame(() => {
      if (isMobile) {
        navigationRef.current
          ?.querySelector<HTMLElement>("a[href], button:not([disabled])")
          ?.focus();
      }
    });

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeMobileMenu(true);
        return;
      }

      if (event.key === "Tab" && isMobile && navigationRef.current) {
        const navigationControls = Array.from(
          navigationRef.current.querySelectorAll<HTMLElement>(
            "a[href], button:not([disabled])",
          ),
        );
        const closeControl = document.querySelector<HTMLElement>(
          ".menu-drawer-scrim",
        );
        const focusableElements = closeControl
          ? [...navigationControls, closeControl]
          : navigationControls;
        const firstElement = focusableElements[0];
        const lastElement = focusableElements.at(-1);

        if (!firstElement || !lastElement) {
          return;
        }
        if (event.shiftKey && document.activeElement === firstElement) {
          event.preventDefault();
          lastElement.focus();
        } else if (!event.shiftKey && document.activeElement === lastElement) {
          event.preventDefault();
          firstElement.focus();
        }
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      window.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = previousBodyOverflow;
      backgroundElements.forEach((element) => {
        if (!previouslyInertElements.has(element)) {
          element.removeAttribute("inert");
        }
      });
    };
  }, [closeMobileMenu, isMenuOpen]);

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
  const brandName = getMunicipalBrandName(user);

  const onboardingSteps: OnboardingStep[] = [
    {
      id: "theme",
      eyebrow: "Tema",
      title: "Claro u oscuro",
      body: "Cambia el modo visual desde el control de la cabecera. Lo recordaremos en este navegador.",
    },
    ...(canUseAssistant
      ? [
          {
            id: "assistant" as const,
            eyebrow: "Anacleto",
            title: "Asistente municipal",
            body: "Abre Anacleto desde la navegación superior para consultar información, organizar trabajo o preparar borradores supervisados.",
          },
        ]
      : []),
    {
      id: "account",
      eyebrow: "Cuenta",
      title: "Tu perfil",
      body: "Entra en Mi cuenta desde la navegación superior para revisar tus datos, organización y permisos.",
    },
  ];

  const safeOnboardingIndex = Math.min(
    activeOnboardingIndex,
    onboardingSteps.length - 1,
  );
  const activeOnboardingStep = onboardingSteps[safeOnboardingIndex];

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

  // La barra municipal acompaña a las pantallas institucionales (ADR-048); la
  // navegación global vive ahora en la cabecera y deja libre todo el ancho.
  const showTopNav = shouldShowTopNav(pathname);
  const activeTopNavSectionId = activeTopNavSectionFor(pathname);

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="app-brand">
          <span className="app-brand-star" aria-hidden="true">
            <img alt="" src="/brand/logo-principal.svg" />
          </span>
          <span className="app-brand-name" title={brandName}>
            {brandName}
          </span>
        </div>
        <button
          aria-controls="app-primary-navigation"
          aria-expanded={isMenuOpen}
          className="menu-toggle"
          onClick={() =>
            isMenuOpen ? closeMobileMenu(false) : setIsMenuOpen(true)
          }
          ref={menuButtonRef}
          type="button"
        >
          Menú
        </button>
        {isMenuOpen ? (
          <button
            aria-label="Cerrar menú"
            className="menu-drawer-scrim"
            onClick={() => closeMobileMenu(true)}
            type="button"
          />
        ) : null}
        <Suspense
          fallback={
            <nav
              aria-busy="true"
              aria-label="Navegación principal"
              className={isMenuOpen ? "app-nav app-nav--open" : "app-nav"}
              id="app-primary-navigation"
              ref={navigationRef}
            />
          }
        >
          <SidebarNavigation
            isCollapsed={false}
            isMenuOpen={isMenuOpen}
            navigationRef={navigationRef}
            onNavigate={() => closeMobileMenu(false)}
            requirementsTotal={requirementsTotal}
            user={user}
          />
        </Suspense>
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
            <span>{dark ? "Modo claro" : "Modo oscuro"}</span>
          </button>
          <div className="app-session-user">
            <p className="muted">{user.full_name}</p>
            {user.organizations && user.organizations.length > 0 ? (
              <p className="app-session-org">{user.organizations[0].name}</p>
            ) : null}
          </div>
          <button
            aria-label="Cerrar sesión"
            className="secondary-button app-session-logout"
            onClick={() => logout()}
            title="Cerrar sesión"
            type="button"
          >
            <LogOut
              aria-hidden="true"
              className="app-session-logout-icon"
              size={17}
              strokeWidth={1.8}
            />
            <span className="app-session-logout-label">Cerrar sesión</span>
          </button>
        </div>
      </header>
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
          <section className="app-onboarding-note">
            <p className="eyebrow">{activeOnboardingStep.eyebrow}</p>
            <h2 id="app-onboarding-title">{activeOnboardingStep.title}</h2>
            <p>{activeOnboardingStep.body}</p>
            {activeOnboardingStep.id === "theme" ? (
              <button
                className="accent-button"
                onClick={handleThemeToggle}
                type="button"
              >
                Probar ahora
              </button>
            ) : null}
            <div
              className="app-onboarding-controls"
              aria-label="Pasos de la ayuda inicial"
            >
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
            <button
              className="app-onboarding-dismiss"
              onClick={dismissOnboarding}
              type="button"
            >
              Cerrar ayuda
            </button>
          </section>
        </div>
      ) : null}
      <div className="app-main">
        <main className={contentClassName}>
          {showTopNav ? (
            <TopBar
              activeSectionId={activeTopNavSectionId}
              canEditCrest={canEditTownHall(user)}
              crestSrc={hasCrest ? townHallShieldUrl(crestVersion) : null}
              municipalityId={user.organizations?.[0]?.municipality?.id ?? null}
              municipalityName={brandName}
              onCrestDrop={(file) => {
                void uploadTownHallShield(file)
                  .then(() => {
                    setHasCrest(true);
                    setCrestVersion((value) => value + 1);
                  })
                  .catch(() => undefined);
              }}
            />
          ) : null}
          {children}
        </main>
      </div>
    </div>
  );
}
