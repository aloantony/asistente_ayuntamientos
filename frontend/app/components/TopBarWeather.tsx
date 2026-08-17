"use client";

import { useEffect, useState } from "react";
import { describeWeatherCode, fetchMunicipalWeather } from "../lib/weather";
import type { MunicipalWeather } from "./types";
import styles from "./TopBar.module.css";

/**
 * Temperatura del municipio en la barra superior.
 *
 * Falla en silencio a propósito (ADR-038): si el municipio no tiene coordenadas
 * oficiales, si el proveedor no responde o si la cuenta no puede leer el
 * municipio, el bloque no se dibuja. Mostrar un error de meteorología en la
 * cabecera institucional daría a un adorno el peso de una avería.
 */
export function TopBarWeather({
  municipalityId,
}: {
  municipalityId: number | null;
}) {
  const [weather, setWeather] = useState<MunicipalWeather | null>(null);

  useEffect(() => {
    if (municipalityId === null) {
      setWeather(null);
      return;
    }

    const controller = new AbortController();
    fetchMunicipalWeather(municipalityId, controller.signal)
      .then((loaded) => {
        if (!controller.signal.aborted) {
          setWeather(loaded);
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setWeather(null);
        }
      });

    return () => controller.abort();
  }, [municipalityId]);

  if (weather === null) {
    return null;
  }

  const description = describeWeatherCode(weather.weather_code);
  const temperature = Math.round(weather.temperature_c);

  return (
    <div className={styles.weather}>
      <strong>{temperature}°</strong>
      <span>
        {description ?? "Ahora"}
        {weather.relative_humidity !== null
          ? ` · ${weather.relative_humidity}%`
          : ""}
      </span>
    </div>
  );
}
