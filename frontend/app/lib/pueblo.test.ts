import { afterEach, describe, expect, it, vi } from "vitest";
import {
  _vaciarCacheCartografia,
  buscarPueblo,
  cargarCartografia,
  limitesDe,
  mascaraExterior,
  nivelDetalle,
  normalizarNombre,
  PUEBLOS,
  vialVisible,
} from "./pueblo";

afterEach(() => {
  _vaciarCacheCartografia();
  vi.unstubAllGlobals();
});

describe("normalizarNombre", () => {
  it("iguala acentos, mayúsculas y separadores", () => {
    expect(normalizarNombre("Fuentelcésped")).toBe("fuentelcesped");
    expect(normalizarNombre("  FUENTELCÉSPED  ")).toBe("fuentelcesped");
    expect(normalizarNombre("Villar de la Yegua")).toBe("villar de la yegua");
  });
});

describe("buscarPueblo", () => {
  it("encuentra el municipio por su código INE", () => {
    expect(buscarPueblo({ name: "Otro nombre", ine_code: "09137" })?.nombre).toBe(
      "Fuentelcésped",
    );
  });

  it("cae al nombre cuando no hay código, aunque venga sin acentuar", () => {
    expect(buscarPueblo({ name: "fuentelcesped" })?.ine).toBe("09137");
  });

  it("un municipio sin cartografía no devuelve nada", () => {
    expect(buscarPueblo({ name: "Aranda de Duero", ine_code: "09018" })).toBe(
      null,
    );
    expect(buscarPueblo(null)).toBe(null);
  });

  it("hoy sólo hay un pueblo cartografiado", () => {
    expect(PUEBLOS).toHaveLength(1);
  });
});

describe("cargarCartografia", () => {
  it("rechaza un plano de otro municipio y permite reintentar", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ ine: "09140" }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ ine: "09137" }) });
    vi.stubGlobal("fetch", fetchMock);
    await expect(cargarCartografia(PUEBLOS[0])).rejects.toThrow();
    await expect(cargarCartografia(PUEBLOS[0])).resolves.toEqual({ ine: "09137" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("descarga una sola vez aunque se pida dos veces", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => ({ ine: "09137" }) });
    vi.stubGlobal("fetch", fetchMock);

    const [primera, segunda] = await Promise.all([
      cargarCartografia(PUEBLOS[0]),
      cargarCartografia(PUEBLOS[0]),
    ]);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(primera).toBe(segunda);
  });

  it("un fallo no envenena la caché: el siguiente intento vuelve a pedirlo", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: false })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ ine: "09137" }) });
    vi.stubGlobal("fetch", fetchMock);

    await expect(cargarCartografia(PUEBLOS[0])).rejects.toThrow(
      /No se pudo cargar el mapa/,
    );
    await expect(cargarCartografia(PUEBLOS[0])).resolves.toEqual({
      ine: "09137",
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe("limitesDe", () => {
  it("encierra el polígono en el orden que espera Leaflet", () => {
    expect(
      limitesDe({
        type: "Polygon",
        coordinates: [
          [
            [-3.67, 41.57],
            [-3.61, 41.57],
            [-3.61, 41.63],
            [-3.67, 41.57],
          ],
        ],
      }),
    ).toEqual([
      [41.57, -3.67],
      [41.63, -3.61],
    ]);
  });

  it("recorre también los multipolígonos", () => {
    expect(
      limitesDe({
        type: "MultiPolygon",
        coordinates: [
          [[[0, 0], [1, 1], [0, 0]]],
          [[[-2, -2], [3, 4], [-2, -2]]],
        ],
      }),
    ).toEqual([
      [-2, -2],
      [4, 3],
    ]);
  });
});

describe("mascaraExterior", () => {
  it("envuelve el mundo y deja el término como agujero", () => {
    const limite = {
      type: "Polygon" as const,
      coordinates: [
        [
          [-3.65, 41.58],
          [-3.63, 41.58],
          [-3.63, 41.6],
          [-3.65, 41.58],
        ],
      ],
    };
    const mascara = mascaraExterior(limite);

    expect(mascara.type).toBe("Polygon");
    expect(mascara.coordinates[0]).toEqual([
      [-180, -85],
      [180, -85],
      [180, 85],
      [-180, 85],
      [-180, -85],
    ]);
    // El anillo del término viaja intacto: es el agujero.
    expect(mascara.coordinates[1]).toEqual(limite.coordinates[0]);
  });

  it("un multipolígono aporta todos sus anillos como agujeros", () => {
    const mascara = mascaraExterior({
      type: "MultiPolygon",
      coordinates: [
        [[[0, 0], [1, 1], [0, 0]]],
        [[[5, 5], [6, 6], [5, 5]]],
      ],
    });

    expect(mascara.coordinates).toHaveLength(3);
  });
});

describe("nivelDetalle", () => {
  it("reparte el zoom en término, pueblo y calle", () => {
    expect(nivelDetalle(12)).toBe("termino");
    expect(nivelDetalle(13.9)).toBe("termino");
    expect(nivelDetalle(14)).toBe("pueblo");
    expect(nivelDetalle(15.9)).toBe("pueblo");
    expect(nivelDetalle(16)).toBe("calle");
    expect(nivelDetalle(19)).toBe("calle");
  });
});

describe("vialVisible", () => {
  it("de lejos sólo la red principal; de cerca hasta los caminos", () => {
    expect(vialVisible("principal", "termino")).toBe(true);
    expect(vialVisible("calle", "termino")).toBe(false);
    expect(vialVisible("calle", "pueblo")).toBe(true);
    expect(vialVisible("camino", "pueblo")).toBe(false);
    expect(vialVisible("camino", "calle")).toBe(true);
  });

  it("una clase desconocida no se dibuja nunca", () => {
    expect(vialVisible("teleférico", "calle")).toBe(false);
    expect(vialVisible(undefined, "calle")).toBe(false);
  });
});


describe("Identidad municipal del plano", () => {
  it("no usa el nombre para ignorar un código INE de otro municipio", () => {
    expect(buscarPueblo({ name: "Fuentelcésped", ine_code: "09140" })).toBeNull();
    expect(buscarPueblo({ name: "Fuentelcésped", ine_code: "09137" })?.ine).toBe("09137");
  });
});
