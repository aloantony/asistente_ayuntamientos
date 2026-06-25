"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { userHasPermission } from "../components/types";
import {
  consumePendingLoginRedirect,
  shouldShowAdminPanel,
  shouldShowRequirementsPanel,
  useSession,
} from "../lib/session";

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

export default function AppLayout({
  children,
}: Readonly<{
  children: ReactNode;
}>) {
  const router = useRouter();
  const pathname = usePathname();
  const { user, isLoadingSession, logout } = useSession();
  const [isMenuOpen, setIsMenuOpen] = useState(false);
  const { dark, toggle: toggleTheme } = useDarkMode();

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

  const navItems = [
    ...(userHasPermission(user, "assistant.use")
      ? [{ href: "/asistente", label: "Asistente" }]
      : []),
    ...(shouldShowRequirementsPanel(user)
      ? [{ href: "/requisitos", label: "Necesidades" }]
      : []),
    { href: "/proyectos", label: "Proyectos" },
    ...(shouldShowAdminPanel(user)
      ? [{ href: "/admin", label: "Administración" }]
      : []),
    { href: "/cuenta", label: "Mi cuenta" },
  ];

  return (
    <div className="app-shell">
      <aside className="app-sidebar">
        <div className="app-brand">
          <span className="app-brand-star" aria-hidden="true">
            <svg
              fill="none"
              height="18"
              stroke="currentColor"
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth="1.7"
              viewBox="0 0 24 24"
              width="18"
            >
              <path d="M12 3 L13.6 10.4 21 12 13.6 13.6 12 21 10.4 13.6 3 12 10.4 10.4 Z" />
            </svg>
          </span>
          <span className="app-brand-name">Asistente Bral</span>
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
          {navItems.map((item) => (
            <Link
              className={pathname.startsWith(item.href) ? "active" : undefined}
              href={item.href}
              key={item.href}
              // Cierra también al pulsar la sección ya activa, donde el
              // pathname no cambia y el efecto de navegación no se dispara.
              onClick={() => setIsMenuOpen(false)}
            >
              {item.label}
            </Link>
          ))}
        </nav>
        <div className="app-session">
          <button
            aria-pressed={dark}
            className="app-theme-toggle"
            onClick={toggleTheme}
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
      <main className="app-content">{children}</main>
    </div>
  );
}
