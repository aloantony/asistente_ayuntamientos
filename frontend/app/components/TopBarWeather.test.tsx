// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MunicipalWeather } from "./types";

const fetchMunicipalWeather = vi.fn();

vi.mock("../lib/weather", async () => {
  const actual =
    await vi.importActual<typeof import("../lib/weather")>("../lib/weather");
  return {
    ...actual,
    fetchMunicipalWeather: (...args: unknown[]) => fetchMunicipalWeather(...args),
  };
});

const { TopBarWeather } = await import("./TopBarWeather");

function weather(overrides: Partial<MunicipalWeather> = {}): MunicipalWeather {
  return {
    temperature_c: 27.4,
    apparent_temperature_c: 29.1,
    relative_humidity: 41,
    wind_speed_kmh: 12.5,
    weather_code: 0,
    is_day: true,
    observed_at: "2026-08-04T18:00",
    latitude: 41.8,
    longitude: -3.5,
    provider: "open-meteo",
    ...overrides,
  };
}

afterEach(cleanup);
beforeEach(() => {
  fetchMunicipalWeather.mockReset().mockResolvedValue(weather());
});

describe("TopBarWeather", () => {
  it("muestra la temperatura redondeada y la descripción", async () => {
    render(<TopBarWeather municipalityId={7} />);

    await waitFor(() => expect(screen.getByText("27°")).toBeTruthy());
    expect(screen.getByText(/Despejado/)).toBeTruthy();
    expect(fetchMunicipalWeather).toHaveBeenCalledWith(7, expect.anything());
  });

  it("no dibuja nada cuando el proveedor falla", async () => {
    // El tiempo es contexto: una avería suya no debe verse en la cabecera
    // institucional como si algo del ayuntamiento estuviera roto.
    fetchMunicipalWeather.mockRejectedValue(new Error("503"));

    const { container } = render(<TopBarWeather municipalityId={7} />);

    await waitFor(() => expect(fetchMunicipalWeather).toHaveBeenCalled());
    expect(container.textContent).toBe("");
  });

  it("no consulta nada sin municipio", () => {
    const { container } = render(<TopBarWeather municipalityId={null} />);

    expect(fetchMunicipalWeather).not.toHaveBeenCalled();
    expect(container.textContent).toBe("");
  });

  it("tolera una lectura sin humedad", async () => {
    fetchMunicipalWeather.mockResolvedValue(
      weather({ relative_humidity: null, weather_code: null }),
    );

    render(<TopBarWeather municipalityId={7} />);

    await waitFor(() => expect(screen.getByText("27°")).toBeTruthy());
    expect(screen.getByText("Ahora")).toBeTruthy();
  });
});
