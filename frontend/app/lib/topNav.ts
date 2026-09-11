// Modelo de la barra superior municipal (ADR-048).
//
// La navegación institucional es FIJA: no se puede renombrar, reordenar ni
// ampliar desde la interfaz. El diseño de referencia incluía un editor de menú,
// pero se descartó de forma deliberada — el menú de un ayuntamiento describe su
// organización, no una preferencia del usuario, y mantenerlo en código lo hace
// revisable, traducible y coherente entre municipios.
//
// Las entradas cuyo destino todavía no existe se declaran igualmente, con
// `enabled: false`: sirven de índice de lo que falta y se activan en la fase que
// construye su pantalla, sin tener que reescribir el modelo.

export type TopNavItem = {
  /** Etiqueta visible en el desplegable. */
  label: string;
  /** Destino; sólo se navega cuando la entrada está activa. */
  href: string;
  /** Falso mientras la pantalla de destino no exista (no se renderiza). */
  enabled: boolean;
};

export type TopNavSection = {
  /** Identificador estable para tests y para marcar la sección activa. */
  id: string;
  /** Rótulo en versales de la píldora de navegación. */
  label: string;
  /** Destino al pulsar la sección; null si sólo abre el desplegable. */
  href: string | null;
  enabled: boolean;
  items: TopNavItem[];
};

export const TOP_NAV_SECTIONS: TopNavSection[] = [
  {
    id: "ayuntamiento",
    label: "AYUNTAMIENTO",
    href: "/ayuntamiento",
    enabled: true,
    items: [
      {
        label: "Información del municipio",
        href: "/ayuntamiento?tab=summary",
        enabled: true,
      },
      { label: "Mapa municipal", href: "/ayuntamiento?tab=map", enabled: true },
    ],
  },
  {
    id: "sede",
    label: "SEDE ELECTRÓNICA",
    href: "/sede",
    enabled: true,
    items: [
      { label: "Tablón de anuncios", href: "/sede?seccion=tablon", enabled: true },
      { label: "Trámites online", href: "/sede?seccion=tramites", enabled: true },
      { label: "Tributos", href: "/sede?seccion=tributos", enabled: true },
      {
        label: "Perfil de contratante",
        href: "/sede?seccion=contratante",
        enabled: true,
      },
      {
        label: "Transparencia",
        href: "/sede?seccion=transparencia",
        enabled: true,
      },
      { label: "Plenos", href: "/sede?seccion=plenos", enabled: true },
      // La normativa completa vive en su propia pantalla, con buscador y
      // comparador; la sede sólo enseña la vigente.
      { label: "Normativa", href: "/ordenanzas", enabled: true },
    ],
  },
];

/** Rutas donde se muestra la barra superior municipal. */
export const TOP_NAV_ROUTES = ["/ayuntamiento", "/sede"];

export function shouldShowTopNav(pathname: string) {
  return TOP_NAV_ROUTES.some(
    (route) => pathname === route || pathname.startsWith(`${route}/`),
  );
}

/**
 * Sección resaltada en la barra. Se deriva sólo de la ruta: las pestañas
 * internas de /ayuntamiento (`?tab=`) todavía comparten pantalla, así que
 * mientras vivan ahí la sección activa es "ayuntamiento".
 */
export function activeTopNavSectionFor(pathname: string): string | null {
  if (pathname === "/sede" || pathname.startsWith("/sede/")) {
    return "sede";
  }
  if (pathname === "/ayuntamiento" || pathname.startsWith("/ayuntamiento/")) {
    return "ayuntamiento";
  }
  return null;
}

/** Secciones visibles: activas, y sin las entradas aún no construidas. */
export function visibleTopNavSections(): TopNavSection[] {
  return TOP_NAV_SECTIONS.filter((section) => section.enabled).map((section) => ({
    ...section,
    items: section.items.filter((item) => item.enabled),
  }));
}
