"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { userHasPermission } from "../components/types";
import {
  consumePendingLoginRedirect,
  shouldShowAdminPanel,
  shouldShowRequirementsPanel,
  useSession,
} from "../lib/session";

export default function AppLayout({
  children,
}: Readonly<{
  children: ReactNode;
}>) {
  const router = useRouter();
  const pathname = usePathname();
  const { user, isLoadingSession, logout } = useSession();
  const [isMenuOpen, setIsMenuOpen] = useState(false);

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
        <p className="app-brand">Asistente Ayuntamientos</p>
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
          <p className="muted">{user.full_name}</p>
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
