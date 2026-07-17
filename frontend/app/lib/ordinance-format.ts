import type { OrdinanceStatus } from "../components/types";

export function formatLegalDate(value: string | null) {
  if (!value) {
    return "No consta";
  }

  const [year, month, day] = value.slice(0, 10).split("-").map(Number);
  if (!year || !month || !day) {
    return value;
  }

  return new Intl.DateTimeFormat("es-ES", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  }).format(new Date(Date.UTC(year, month - 1, day)));
}

export function formatPopulation(value: number | null) {
  if (value === null) {
    return "Población no informada";
  }

  return `${new Intl.NumberFormat("es-ES").format(value)} habitantes`;
}

export function ordinanceStatusNote(status: OrdinanceStatus) {
  switch (status) {
    case "active":
      return null;
    case "partially_repealed":
      return "Vigencia parcial: comprueba qué artículos siguen aplicándose en la publicación oficial.";
    case "unknown":
      return "Vigencia no verificada: contrasta el texto y sus modificaciones en la fuente oficial.";
    case "repealed":
      return "Norma derogada: se muestra únicamente porque se incluyeron estados inactivos.";
    case "superseded":
      return "Norma sustituida: consulta la regulación posterior antes de utilizarla.";
    case "archived":
      return "Registro archivado: se conserva solo como referencia histórica.";
  }
}

export function safeExternalUrl(value: string | null) {
  if (!value) {
    return null;
  }

  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" || parsed.protocol === "http:"
      ? parsed.toString()
      : null;
  } catch {
    return null;
  }
}
