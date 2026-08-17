"use client";

import { BarChart, LineChart, type SeriesPoint } from "./SeriesChart";
import { SectionShell } from "./SectionShell";
import type { ClimateRecord, HouseholdStat, PadronRecord } from "../types";
import styles from "./SeriesMunicipio.module.css";

const MONTH_LABELS = [
  "E",
  "F",
  "M",
  "A",
  "M",
  "J",
  "J",
  "A",
  "S",
  "O",
  "N",
  "D",
];

function toNumber(value: string | null) {
  if (value === null) {
    return null;
  }
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** El backend devuelve la serie del año más reciente primero; el eje va al revés. */
function chronological<T extends { reference_year: number }>(rows: T[]) {
  return [...rows].sort((left, right) => left.reference_year - right.reference_year);
}

export function SeriesMunicipio({
  padron,
  climate,
  households,
}: {
  padron: PadronRecord[];
  climate: ClimateRecord[];
  households: HouseholdStat[];
}) {
  const padronPoints: SeriesPoint[] = chronological(padron).map((row) => ({
    label: String(row.reference_year),
    value: row.population,
  }));

  // Del clima interesa el detalle mensual del año más reciente que lo tenga;
  // la fila anual es un resumen y no dibuja una curva.
  const monthlyYear = climate.find((row) => row.reference_month !== null)
    ?.reference_year;
  const monthly = climate
    .filter(
      (row) => row.reference_year === monthlyYear && row.reference_month !== null,
    )
    .sort((left, right) => (left.reference_month ?? 0) - (right.reference_month ?? 0));

  const temperaturePoints: SeriesPoint[] = monthly.flatMap((row) => {
    const value = toNumber(row.avg_temperature_c);
    return value === null
      ? []
      : [{ label: MONTH_LABELS[(row.reference_month ?? 1) - 1], value }];
  });
  const rainPoints: SeriesPoint[] = monthly.flatMap((row) => {
    const value = toNumber(row.precipitation_mm);
    return value === null
      ? []
      : [{ label: MONTH_LABELS[(row.reference_month ?? 1) - 1], value }];
  });

  const latestHouseholds = chronological(households).at(-1) ?? null;
  const dwellingPoints: SeriesPoint[] = latestHouseholds
    ? [
        { label: "Principales", value: latestHouseholds.primary_dwellings ?? 0 },
        {
          label: "Secundarias",
          value: latestHouseholds.secondary_dwellings ?? 0,
        },
        { label: "Vacías", value: latestHouseholds.empty_dwellings ?? 0 },
      ].filter((point) => point.value > 0)
    : [];

  const hasAnything =
    padronPoints.length >= 2 ||
    temperaturePoints.length > 0 ||
    rainPoints.length > 0 ||
    dwellingPoints.length > 0;

  const count =
    padron.length + climate.length + households.length || null;

  return (
    <SectionShell
      count={count}
      sectionKey="series-municipio"
      title="Series del municipio"
    >
      {hasAnything ? (
        <div className={styles.grid}>
          <LineChart
            points={padronPoints}
            title="Empadronamiento"
            unit="hab."
          />
          <LineChart
            points={temperaturePoints}
            title={`Temperatura media${monthlyYear ? ` · ${monthlyYear}` : ""}`}
            unit="°C"
          />
          <BarChart
            points={rainPoints}
            title={`Precipitación${monthlyYear ? ` · ${monthlyYear}` : ""}`}
            unit="mm"
          />
          <BarChart
            points={dwellingPoints}
            title={`Viviendas${
              latestHouseholds ? ` · ${latestHouseholds.reference_year}` : ""
            }`}
          />
        </div>
      ) : (
        <p className={styles.empty}>
          Todavía no hay series registradas. El empadronamiento, el clima y el
          parque de viviendas se cargan desde los datos del municipio.
        </p>
      )}
    </SectionShell>
  );
}
