"use client";

import { useId } from "react";
import styles from "./SeriesChart.module.css";

// Gráficas SVG propias, sin librería nueva (ADR-048 sobre no ampliar el peso
// del bundle sin motivo). Son series de pocos puntos —un valor por año o por
// mes—, así que una línea y unas barras cubren todo lo que la pantalla pide.

export type SeriesPoint = {
  /** Rótulo del eje horizontal: el año o el mes. */
  label: string;
  value: number;
};

function niceBounds(values: number[]) {
  const min = Math.min(...values);
  const max = Math.max(...values);
  if (min === max) {
    // Una serie plana necesita margen o la línea se pega al borde.
    return { min: min - 1, max: max + 1 };
  }
  const padding = (max - min) * 0.12;
  return { min: min - padding, max: max + padding };
}

/** Serie continua: población a lo largo de los años, temperatura por mes. */
export function LineChart({
  points,
  title,
  unit = "",
  height = 140,
}: {
  points: SeriesPoint[];
  title: string;
  unit?: string;
  height?: number;
}) {
  const titleId = useId();
  if (points.length < 2) {
    return null;
  }

  const width = 480;
  const padding = { top: 10, right: 8, bottom: 22, left: 8 };
  const bounds = niceBounds(points.map((point) => point.value));
  const span = bounds.max - bounds.min;
  const innerWidth = width - padding.left - padding.right;
  const innerHeight = height - padding.top - padding.bottom;

  const coordinates = points.map((point, index) => {
    const x = padding.left + (index / (points.length - 1)) * innerWidth;
    const y =
      padding.top + innerHeight - ((point.value - bounds.min) / span) * innerHeight;
    return { ...point, x, y };
  });
  const path = coordinates
    .map((point, index) => `${index === 0 ? "M" : "L"}${point.x} ${point.y}`)
    .join(" ");
  const last = coordinates[coordinates.length - 1];

  return (
    <figure className={styles.chart}>
      <figcaption className={styles.caption} id={titleId}>
        {title}
      </figcaption>
      <svg
        aria-labelledby={titleId}
        className={styles.svg}
        role="img"
        viewBox={`0 0 ${width} ${height}`}
      >
        <path className={styles.line} d={path} />
        <circle className={styles.lastPoint} cx={last.x} cy={last.y} r="3.5" />
        {coordinates.map((point, index) =>
          // Solo el primero y el último llevan rótulo: con una serie larga se
          // solaparían y no hay espacio para rotarlos.
          index === 0 || index === coordinates.length - 1 ? (
            <text
              className={styles.axisLabel}
              key={point.label}
              textAnchor={index === 0 ? "start" : "end"}
              x={point.x}
              y={height - 6}
            >
              {point.label}
            </text>
          ) : null,
        )}
      </svg>
      <p className={styles.reading}>
        {new Intl.NumberFormat("es-ES", { maximumFractionDigits: 1 }).format(
          last.value,
        )}
        {unit ? ` ${unit}` : ""} <span>en {last.label}</span>
      </p>
    </figure>
  );
}

/** Serie discreta: precipitación mensual, reparto del parque de viviendas. */
export function BarChart({
  points,
  title,
  unit = "",
  height = 140,
}: {
  points: SeriesPoint[];
  title: string;
  unit?: string;
  height?: number;
}) {
  const titleId = useId();
  if (points.length === 0) {
    return null;
  }

  const width = 480;
  const padding = { top: 10, right: 8, bottom: 22, left: 8 };
  const innerWidth = width - padding.left - padding.right;
  const innerHeight = height - padding.top - padding.bottom;
  const max = Math.max(...points.map((point) => point.value), 1);
  const slot = innerWidth / points.length;
  const barWidth = Math.max(slot * 0.6, 2);

  return (
    <figure className={styles.chart}>
      <figcaption className={styles.caption} id={titleId}>
        {title}
      </figcaption>
      <svg
        aria-labelledby={titleId}
        className={styles.svg}
        role="img"
        viewBox={`0 0 ${width} ${height}`}
      >
        {points.map((point, index) => {
          const barHeight = (point.value / max) * innerHeight;
          return (
            <rect
              className={styles.bar}
              height={Math.max(barHeight, 1)}
              key={point.label}
              rx="2"
              width={barWidth}
              x={padding.left + index * slot + (slot - barWidth) / 2}
              y={padding.top + innerHeight - Math.max(barHeight, 1)}
            />
          );
        })}
        {points.map((point, index) =>
          index === 0 || index === points.length - 1 ? (
            <text
              className={styles.axisLabel}
              key={`label-${point.label}`}
              textAnchor={index === 0 ? "start" : "end"}
              x={padding.left + index * slot + slot / 2}
              y={height - 6}
            >
              {point.label}
            </text>
          ) : null,
        )}
      </svg>
      <p className={styles.reading}>
        Máximo{" "}
        {new Intl.NumberFormat("es-ES", { maximumFractionDigits: 1 }).format(max)}
        {unit ? ` ${unit}` : ""}
      </p>
    </figure>
  );
}
