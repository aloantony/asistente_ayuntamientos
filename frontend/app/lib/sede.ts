import type { SedeContent } from "../components/types";
import { adminRequest } from "./api";

/**
 * La sede llega entera en una respuesta: la de un municipio pequeño cabe de
 * sobra, y pedir siete veces para pintar siete pestañas sería derrochar.
 */
export function fetchSedeContent(organizationId: number, signal?: AbortSignal) {
  const params = new URLSearchParams({
    organization_id: String(organizationId),
  });

  return adminRequest<SedeContent>(
    `/sede?${params.toString()}`,
    "",
    "No se pudo cargar la sede electrónica.",
    { signal },
  );
}

/** Las secciones son las mismas del menú superior, en el mismo orden. */
export const SEDE_SECTIONS = [
  { key: "tablon", label: "Tablón de anuncios" },
  { key: "tramites", label: "Trámites" },
  { key: "tributos", label: "Tributos" },
  { key: "contratante", label: "Perfil de contratante" },
  { key: "transparencia", label: "Transparencia" },
  { key: "plenos", label: "Plenos" },
  { key: "normativa", label: "Normativa" },
] as const;

export type SedeSectionKey = (typeof SEDE_SECTIONS)[number]["key"];

export function countFor(content: SedeContent | null, key: SedeSectionKey) {
  if (content === null) {
    return null;
  }
  switch (key) {
    case "tablon":
      return content.board.length;
    case "tramites":
      return content.procedures.length;
    case "tributos":
      return content.taxes.length;
    case "contratante":
      return content.contracts.length;
    case "transparencia":
      return content.transparency.length;
    case "plenos":
      return content.sessions.length;
    case "normativa":
      return content.ordinances.length;
  }
}
