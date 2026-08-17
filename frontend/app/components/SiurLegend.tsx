"use client";

import { useState } from "react";

type SiurLegendProps = {
  title: string;
  url: string;
};

export function SiurLegend({ title, url }: SiurLegendProps) {
  const [expanded, setExpanded] = useState(false);
  const [failed, setFailed] = useState(false);

  return (
    <div className="siur-layer-legend">
      <button
        aria-expanded={expanded}
        className="siur-layer-legend-toggle"
        onClick={() => {
          setExpanded((current) => !current);
          setFailed(false);
        }}
        type="button"
      >
        {expanded ? "Ocultar leyenda" : "Ver leyenda"}
      </button>
      {expanded ? (
        failed ? (
          <p role="status">La leyenda no está disponible ahora mismo.</p>
        ) : (
          // The browser must request this authenticated same-origin image
          // directly so its httpOnly session cookie is included.
          // eslint-disable-next-line @next/next/no-img-element
          <img
            alt={`Leyenda de ${title}`}
            loading="lazy"
            onError={() => setFailed(true)}
            src={url}
          />
        )
      ) : null}
    </div>
  );
}
