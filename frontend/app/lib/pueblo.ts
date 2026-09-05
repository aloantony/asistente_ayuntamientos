/**
 * Cartografía propia del pueblo.
 *
 * Un mapa del municipio servido, dibujado desde datos que viajan con la
 * aplicación: huellas de edificio del Catastro (INSPIRE Buildings) y viales,
 * agua y usos del suelo de OpenStreetMap, ya reproyectados a WGS84. No hay
 * teselas externas, ni WMS, ni espejo que mantener: el mapa es un JSON estático
 * que el navegador pide una vez.
 *
 * Cada municipio contratado añade su fichero a `public/cartografia/` y su
 * entrada en `PUEBLOS`. Un municipio sin cartografía no rompe nada: la pantalla
 * lo dice y se queda sin mapa hasta que se prepare.
 */

export type Punto = [number, number];

export type PoligonoGeoJson = {
  type: "Polygon" | "MultiPolygon";
  coordinates: number[][][] | number[][][][];
};

export type ColeccionGeoJson = {
  type: "FeatureCollection";
  features: {
    type: "Feature";
    properties: Record<string, unknown>;
    geometry: { type: string; coordinates: unknown };
  }[];
};

export type CartografiaPueblo = {
  municipio: string;
  provincia: string;
  ine: string;
  /** Centro del casco urbano, en orden GeoJSON: [longitud, latitud]. */
  centro: Punto;
  limite: PoligonoGeoJson;
  edificios: ColeccionGeoJson;
  viales: ColeccionGeoJson;
  agua: ColeccionGeoJson;
  verde: ColeccionGeoJson;
  etiquetas: ColeccionGeoJson;
};

export type PuebloDisponible = {
  ine: string;
  nombre: string;
  archivo: string;
};

/** Municipios con cartografía preparada. Crece con cada contratación. */
export const PUEBLOS: PuebloDisponible[] = [
  {
    ine: "09140",
    nombre: "Fuentelcésped",
    archivo: "/cartografia/fuentelcesped.json",
  },
];

/**
 * Nombre comparable: sin acentos, sin mayúsculas y sin adornos.
 *
 * El nombre del municipio llega de sitios distintos (padrón del INE, alta
 * manual, el propio callejero) y no siempre acentuado igual, así que la
 * comparación no puede ser literal.
 */
export function normalizarNombre(valor: string) {
  return valor
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

/**
 * Lo mínimo que hace falta para reconocer un municipio. Encaja tanto con
 * `Municipality` como con el resumen que trae el selector de contexto, sin que
 * este módulo dependa de ninguno de los dos.
 */
export type MunicipioIdentificable = {
  name?: string | null;
  ine_code?: string | null;
};

/**
 * Busca la cartografía de un municipio por código INE y, si no lo hay, por
 * nombre. El código manda: es lo único que identifica un municipio sin
 * ambigüedad.
 */
export function buscarPueblo(
  municipio: MunicipioIdentificable | null,
): PuebloDisponible | null {
  if (!municipio) {
    return null;
  }
  const ine = municipio.ine_code?.trim();
  if (ine) {
    const porCodigo = PUEBLOS.find((pueblo) => pueblo.ine === ine);
    if (porCodigo) {
      return porCodigo;
    }
  }
  const nombre = municipio.name ? normalizarNombre(municipio.name) : "";
  if (!nombre) {
    return null;
  }
  return (
    PUEBLOS.find((pueblo) => normalizarNombre(pueblo.nombre) === nombre) ?? null
  );
}

const cache = new Map<string, Promise<CartografiaPueblo>>();

/**
 * Descarga la cartografía una sola vez por sesión. El fichero es estático y no
 * cambia mientras la pestaña vive, así que se cachea la promesa: dos paneles
 * abiertos a la vez comparten una única petición.
 */
export function cargarCartografia(
  pueblo: PuebloDisponible,
): Promise<CartografiaPueblo> {
  const enCurso = cache.get(pueblo.archivo);
  if (enCurso) {
    return enCurso;
  }
  const peticion = fetch(pueblo.archivo)
    .then((respuesta) => {
      if (!respuesta.ok) {
        throw new Error(`No se pudo cargar el mapa de ${pueblo.nombre}.`);
      }
      return respuesta.json() as Promise<CartografiaPueblo>;
    })
    .catch((motivo: unknown) => {
      // Un fallo no puede dejar la promesa rota en la caché para siempre: el
      // siguiente intento tiene que poder volver a pedirlo.
      cache.delete(pueblo.archivo);
      throw motivo;
    });
  cache.set(pueblo.archivo, peticion);
  return peticion;
}

/** Sólo para las pruebas: vacía la caché de descargas. */
export function _vaciarCacheCartografia() {
  cache.clear();
}

/** Rectángulo que encierra un polígono, en el orden que espera Leaflet. */
export function limitesDe(
  poligono: PoligonoGeoJson,
): [[number, number], [number, number]] {
  let sur = Infinity;
  let oeste = Infinity;
  let norte = -Infinity;
  let este = -Infinity;

  const recorrer = (nodo: unknown) => {
    if (
      Array.isArray(nodo) &&
      typeof nodo[0] === "number" &&
      typeof nodo[1] === "number"
    ) {
      const [longitud, latitud] = nodo as Punto;
      oeste = Math.min(oeste, longitud);
      este = Math.max(este, longitud);
      sur = Math.min(sur, latitud);
      norte = Math.max(norte, latitud);
      return;
    }
    if (Array.isArray(nodo)) {
      nodo.forEach(recorrer);
    }
  };

  recorrer(poligono.coordinates);
  return [
    [sur, oeste],
    [norte, este],
  ];
}

/**
 * Polígono que tapa todo menos el término municipal.
 *
 * Los datos de OSM llegan con las geometrías enteras: una carretera que cruza
 * el término sigue hasta el pueblo vecino y un monte no se corta en el mojón.
 * Recortar geometría contra un límite cóncavo es trabajo fino y frágil; pintar
 * por encima un rectángulo con el término como agujero consigue lo mismo, con
 * el borde exacto y sin tocar los datos. Leaflet rellena con `evenodd`, así que
 * los anillos interiores son agujeros sin depender del sentido de giro.
 */
export function mascaraExterior(limite: PoligonoGeoJson): PoligonoGeoJson {
  const mundo = [
    [-180, -85],
    [180, -85],
    [180, 85],
    [-180, 85],
    [-180, -85],
  ];
  const anillos =
    limite.type === "Polygon"
      ? (limite.coordinates as number[][][])
      : (limite.coordinates as number[][][][]).flat();
  return { type: "Polygon", coordinates: [mundo, ...anillos] };
}

export type NivelDetalle = "termino" | "pueblo" | "calle";

/**
 * Cuánto se enseña a cada zoom.
 *
 * Un mapa minimalista no dibuja todo siempre: de lejos sólo el término, sus
 * aguas y las carreteras; al acercarse aparecen las calles y los edificios; y
 * sólo de cerca los caminos y accesos, que de lejos serían maraña.
 */
export function nivelDetalle(zoom: number): NivelDetalle {
  if (zoom < 14) {
    return "termino";
  }
  if (zoom < 16) {
    return "pueblo";
  }
  return "calle";
}

const VIALES_POR_NIVEL: Record<NivelDetalle, string[]> = {
  termino: ["principal", "secundaria"],
  pueblo: ["principal", "secundaria", "calle"],
  calle: ["principal", "secundaria", "calle", "servicio", "camino"],
};

export function vialVisible(clase: unknown, nivel: NivelDetalle) {
  return (
    typeof clase === "string" && VIALES_POR_NIVEL[nivel].includes(clase)
  );
}
