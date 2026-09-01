// Tipos compartidos por la pantalla del ayuntamiento y sus secciones.

import type { MunicipalitySummary, OrganizationSummary } from "../types";

/** Identificadores de las sub-pestañas; viajan en los enlaces `?tab=`. */
export type WorkspaceTab =
  | "summary"
  | "administration"
  | "map"
  | "people"
  // Fuera de la fila desde que ésta se ajustó a las cuatro del diseño, pero
  // siguen siendo valores válidos de `?tab=`: los enlaces antiguos llevan a la
  // ruta donde vive ahora ese contenido.
  | "ordinances"
  | "roadmap";

export type MunicipalContext = {
  organization: OrganizationSummary;
  municipality: MunicipalitySummary;
};

/** Error por recurso: cada bloque informa del suyo sin tumbar la pantalla. */
export type ResourceErrors = {
  ordinances: string;
  assets: string;
  maintenance: string;
  government: string;
  staff: string;
};
