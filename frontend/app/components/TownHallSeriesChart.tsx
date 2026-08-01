"use client";

import { useId, useState } from "react";
import type { TownHallContentItem } from "./types";

type TownHallSeriesChartProps = {
  unit: string;
  series: TownHallContentItem[];
  /** Barras para una serie suelta y contable (precipitación); línea si no. */
  asBars?: boolean;
};

const PLOT = { width: 720, height: 240, left: 52, right: 16, top: 16, bottom: 28 };

function niceCeiling(value: number) {
  if (value <= 0) {
    return 1;
  }
  const magnitude = 10 ** Math.floor(Math.log10(value));
  return Math.ceil(value / magnitude) * magnitude;
}

/**
 * Gráfica de series del Ayuntamiento, dibujada a mano en SVG.
 *
 * Un solo eje vertical por gráfica: quien la monta agrupa las series por
 * unidad, de modo que dos magnitudes distintas nunca comparten dibujo. Los
 * colores son los validados en docs/diseno-ayuntamiento-prototipo.md y viven
 * en styles.css, para que el modo oscuro tenga sus propios pasos y no una
 * inversión automática.
 */
export function TownHallSeriesChart({
  unit,
  series,
  asBars = false,
}: TownHallSeriesChartProps) {
  const titleId = useId();
  const [hover, setHover] = useState<{ index: number } | null>(null);
  const [showTable, setShowTable] = useState(false);

  // Las abscisas son etiquetas: se toma el orden de la serie más completa para
  // que dos series con años distintos sigan alineadas.
  const labels: string[] = [];
  for (const item of series) {
    for (const point of item.points) {
      if (!labels.includes(point.x)) {
        labels.push(point.x);
      }
    }
  }

  if (labels.length === 0) {
    return (
      <p className="muted">
        Esta serie todavía no tiene datos. Añádelos para ver la gráfica.
      </p>
    );
  }

  const values = series.flatMap((item) => item.points.map((point) => point.y));
  const maxValue = niceCeiling(Math.max(...values, 0));
  const minValue = Math.min(...values, 0);
  const span = maxValue - minValue || 1;

  const plotWidth = PLOT.width - PLOT.left - PLOT.right;
  const plotHeight = PLOT.height - PLOT.top - PLOT.bottom;
  const step = labels.length > 1 ? plotWidth / (labels.length - 1) : 0;

  function xFor(index: number) {
    return labels.length > 1
      ? PLOT.left + index * step
      : PLOT.left + plotWidth / 2;
  }

  function yFor(value: number) {
    return PLOT.top + plotHeight - ((value - minValue) / span) * plotHeight;
  }

  // Cuatro marcas bastan: una rejilla densa compite con los datos.
  const ticks = [0, 0.25, 0.5, 0.75, 1].map(
    (fraction) => minValue + fraction * span,
  );
  // Con muchas abscisas solo se rotula una de cada n, para que no se pisen.
  const labelEvery = Math.ceil(labels.length / 8);

  return (
    <figure className="townhall-chart">
      <svg
        aria-labelledby={titleId}
        className="townhall-chart-svg"
        onMouseLeave={() => setHover(null)}
        preserveAspectRatio="xMidYMid meet"
        role="img"
        viewBox={`0 0 ${PLOT.width} ${PLOT.height}`}
      >
        <title id={titleId}>
          {series.map((item) => item.title).join(", ")} en {unit}
        </title>

        {ticks.map((tick) => (
          <g key={tick}>
            <line
              className="townhall-chart-grid"
              x1={PLOT.left}
              x2={PLOT.width - PLOT.right}
              y1={yFor(tick)}
              y2={yFor(tick)}
            />
            <text
              className="townhall-chart-axis"
              dominantBaseline="middle"
              textAnchor="end"
              x={PLOT.left - 8}
              y={yFor(tick)}
            >
              {Math.round(tick)}
            </text>
          </g>
        ))}

        {labels.map((label, index) =>
          index % labelEvery === 0 ? (
            <text
              className="townhall-chart-axis"
              key={label}
              textAnchor="middle"
              x={xFor(index)}
              y={PLOT.height - 8}
            >
              {label}
            </text>
          ) : null,
        )}

        {asBars
          ? series[0]?.points.map((point) => {
              const index = labels.indexOf(point.x);
              const barWidth = Math.max(4, step - 2);
              const top = yFor(point.y);
              return (
                <rect
                  className="townhall-chart-bar"
                  height={Math.max(2, yFor(minValue) - top)}
                  key={point.x}
                  rx={4}
                  ry={4}
                  width={barWidth}
                  x={xFor(index) - barWidth / 2}
                  y={top}
                />
              );
            })
          : series.map((item, seriesIndex) => (
              <g data-slot={seriesIndex + 1} key={item.id}>
                <polyline
                  className="townhall-chart-line"
                  points={item.points
                    .map(
                      (point) =>
                        `${xFor(labels.indexOf(point.x))},${yFor(point.y)}`,
                    )
                    .join(" ")}
                />
                {item.points.length === 1 ? (
                  <circle
                    className="townhall-chart-dot"
                    cx={xFor(labels.indexOf(item.points[0].x))}
                    cy={yFor(item.points[0].y)}
                    r={5}
                  />
                ) : null}
              </g>
            ))}

        {/* Zonas de contacto anchas: el objetivo no es la línea, es la columna. */}
        {labels.map((label, index) => (
          <rect
            className="townhall-chart-hit"
            height={plotHeight}
            key={label}
            onMouseEnter={() => setHover({ index })}
            width={Math.max(step, 8)}
            x={xFor(index) - Math.max(step, 8) / 2}
            y={PLOT.top}
          />
        ))}

        {hover ? (
          <line
            className="townhall-chart-crosshair"
            x1={xFor(hover.index)}
            x2={xFor(hover.index)}
            y1={PLOT.top}
            y2={PLOT.top + plotHeight}
          />
        ) : null}
      </svg>

      {hover ? (
        <div aria-live="polite" className="townhall-chart-tooltip">
          <strong>{labels[hover.index]}</strong>
          {series.map((item, seriesIndex) => {
            const point = item.points.find(
              (candidate) => candidate.x === labels[hover.index],
            );
            return point ? (
              <span data-slot={seriesIndex + 1} key={item.id}>
                {item.title}: {point.y} {unit}
              </span>
            ) : null;
          })}
        </div>
      ) : null}

      <figcaption className="townhall-chart-legend">
        {series.length > 1
          ? series.map((item, seriesIndex) => (
              <span data-slot={seriesIndex + 1} key={item.id}>
                {item.title}
              </span>
            ))
          : null}
        <button onClick={() => setShowTable((shown) => !shown)} type="button">
          {showTable ? "Ocultar la tabla" : "Ver los datos en tabla"}
        </button>
      </figcaption>

      {showTable ? (
        <table className="townhall-chart-table">
          <caption>
            {series.map((item) => item.title).join(", ")} ({unit})
          </caption>
          <thead>
            <tr>
              <th scope="col">Periodo</th>
              {series.map((item) => (
                <th key={item.id} scope="col">
                  {item.title}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {labels.map((label) => (
              <tr key={label}>
                <th scope="row">{label}</th>
                {series.map((item) => (
                  <td key={item.id}>
                    {item.points.find((point) => point.x === label)?.y ?? "—"}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </figure>
  );
}
