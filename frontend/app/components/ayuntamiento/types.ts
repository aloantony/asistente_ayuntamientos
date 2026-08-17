// Tipos compartidos por la pantalla del ayuntamiento y sus secciones.

import type { MunicipalitySummary, OrganizationSummary } from "../types";

/** Identificadores de las sub-pestañas; viajan en los enlaces `?tab=`. */
export type WorkspaceTab =
  | "summary"
  | "ordinances"
  | "facilities"
  | "people"
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
