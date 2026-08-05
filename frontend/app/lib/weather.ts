import type { MunicipalWeather } from "../components/types";
import { adminRequest } from "./api";

/**
 * El tiempo del municipio. Falla en silencio a propósito: es contexto, no un
 * dato del que dependa ninguna decisión, así que cuando no está el bloque
 * simplemente no se dibuja en lugar de mostrar un error.
 */
export function fetchMunicipalWeather(
  municipalityId: number,
  signal?: AbortSignal,
) {
  return adminRequest<MunicipalWeather>(
    `/municipalities/${municipalityId}/weather`,
    "",
    "No se pudo consultar el tiempo del municipio.",
    { signal },
  );
}

/** Rótulos de los códigos WMO que devuelve Open-Meteo, agrupados. */
export function describeWeatherCode(code: number | null) {
  if (code === null) {
    return null;
  }
  if (code === 0) return "Despejado";
  if (code <= 3) return "Nubes";
  if (code <= 48) return "Niebla";
  if (code <= 57) return "Llovizna";
  if (code <= 67) return "Lluvia";
  if (code <= 77) return "Nieve";
  if (code <= 82) return "Chubascos";
  if (code <= 86) return "Chubascos de nieve";
  return "Tormenta";
}
