"use client";

import { useEffect, useRef, useState } from "react";
import type {
  GeoJSON as GeoJsonLayer,
  LayerGroup,
  Map as LeafletMap,
} from "leaflet";
import {
  limitesDe,
  mascaraExterior,
  nivelDetalle,
  vialVisible,
  type CartografiaPueblo,
  type NivelDetalle,
} from "../lib/pueblo";
import type { GeoMapItem } from "./types";
import styles from "./PuebloMap.module.css";

type PuebloMapProps = {
  cartografia: CartografiaPueblo;
  items?: GeoMapItem[];
  markerColors?: Record<string, string>;
  selectedItemId?: string | null;
  onSelectItem?: (item: GeoMapItem) => void;
};

const ZOOM_MAXIMO = 19;
const COLOR_MARCADOR = "#3caf8c";
const ATRIBUCION =
  'Edificios: <a href="https://www.catastro.hacienda.gob.es/">Catastro</a> &middot; ' +
  'Viales: &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';

/** Clave estable de un elemento: el par entidad/id, no el índice de la lista. */
export function itemKey(item: GeoMapItem) {
  return `${item.entity_type}:${item.entity_id}`;
}

/** Datos GeoJSON de nuestra cartografía, como los espera Leaflet. */
function comoGeoJson(valor: unknown) {
  return valor as unknown as GeoJSON.GeoJsonObject;
}

export function PuebloMap({
  cartografia,
  items = [],
  markerColors = {},
  onSelectItem,
  selectedItemId = null,
}: PuebloMapProps) {
  const contenedorRef = useRef<HTMLDivElement | null>(null);
  const mapaRef = useRef<LeafletMap | null>(null);
  const marcadoresRef = useRef<LayerGroup | null>(null);
  const seleccionRef = useRef<((item: GeoMapItem) => void) | undefined>(
    onSelectItem,
  );
  const [nivel, setNivel] = useState<NivelDetalle>("pueblo");
  const [listo, setListo] = useState(false);

  // El callback cambia en cada render del padre; guardarlo en una ref evita
  // rehacer todos los marcadores sólo por eso.
  seleccionRef.current = onSelectItem;

  // Dibujo del pueblo. Se monta una vez por cartografía: los datos son
  // estáticos, así que no hay nada que redibujar salvo que cambie el municipio.
  useEffect(() => {
    const contenedor = contenedorRef.current;
    if (!contenedor) {
      return;
    }

    let cancelado = false;
    let mapa: LeafletMap | null = null;
    let observador: ResizeObserver | null = null;

    import("leaflet").then((L) => {
      if (cancelado) {
        return;
      }

      const limites = L.latLngBounds(limitesDe(cartografia.limite));
      // Sin capa de teselas: el fondo es el propio dibujo del término. Fuera
      // del municipio no hay nada que enseñar, y por eso el mapa no deja salir.
      const instancia = L.map(contenedor, {
        attributionControl: true,
        zoomControl: true,
        maxZoom: ZOOM_MAXIMO,
        maxBounds: limites.pad(0.25),
        maxBoundsViscosity: 0.9,
      });
      mapa = instancia;
      mapaRef.current = instancia;
      instancia.attributionControl.addAttribution(ATRIBUCION);

      const pane = (nombre: string, orden: number) => {
        instancia.createPane(nombre).style.zIndex = String(orden);
        return nombre;
      };

      L.geoJSON(comoGeoJson(cartografia.limite), {
        pane: pane("pueblo-termino", 380),
        style: { className: styles.termino },
      }).addTo(instancia);

      L.geoJSON(comoGeoJson(cartografia.verde), {
        pane: pane("pueblo-verde", 390),
        style: (feature) => ({
          className: [
            styles.suelo,
            styles[`suelo-${String(feature?.properties?.tipo)}`] ?? "",
          ]
            .filter(Boolean)
            .join(" "),
        }),
      }).addTo(instancia);

      L.geoJSON(comoGeoJson(cartografia.agua), {
        pane: pane("pueblo-agua", 400),
        style: { className: styles.agua },
      }).addTo(instancia);

      const paneViales = pane("pueblo-viales", 410);
      let nivelDibujado = nivelDetalle(instancia.getZoom());
      const viales: GeoJsonLayer = L.geoJSON(comoGeoJson(cartografia.viales), {
        pane: paneViales,
        style: (feature) => ({
          className: [
            styles.vial,
            styles[`vial-${String(feature?.properties?.clase)}`] ?? "",
          ]
            .filter(Boolean)
            .join(" "),
        }),
        filter: (feature) =>
          vialVisible(feature?.properties?.clase, nivelDibujado),
      });

      L.geoJSON(comoGeoJson(cartografia.edificios), {
        pane: pane("pueblo-edificios", 420),
        style: { className: styles.edificio },
      }).addTo(instancia);

      // Todo lo que sobresale del término se tapa aquí, y el borde se dibuja
      // encima de la máscara para que quede limpio.
      L.geoJSON(comoGeoJson(mascaraExterior(cartografia.limite)), {
        pane: pane("pueblo-mascara", 430),
        interactive: false,
        style: { className: styles.mascara },
      }).addTo(instancia);

      L.geoJSON(comoGeoJson(cartografia.limite), {
        pane: pane("pueblo-borde", 440),
        interactive: false,
        style: { className: styles.borde },
      }).addTo(instancia);

      marcadoresRef.current = L.layerGroup().addTo(instancia);

      instancia.fitBounds(limites, { padding: [16, 16] });
      // Alejarse más que el término entero sólo enseña vacío.
      instancia.setMinZoom(instancia.getZoom() - 1);

      nivelDibujado = nivelDetalle(instancia.getZoom());
      viales.addTo(instancia);
      setNivel(nivelDibujado);

      instancia.on("zoomend", () => {
        const siguiente = nivelDetalle(instancia.getZoom());
        if (siguiente === nivelDibujado) {
          return;
        }
        nivelDibujado = siguiente;
        setNivel(siguiente);
        // El filtro de Leaflet sólo actúa al añadir datos, así que la capa de
        // viales se rehace con los que tocan a este zoom.
        viales.clearLayers();
        viales.addData(comoGeoJson(cartografia.viales));
      });

      // El panel cambia de tamaño al plegar la barra lateral o al girar el
      // móvil, y Leaflet no se entera solo.
      observador = new ResizeObserver(() => instancia.invalidateSize());
      observador.observe(contenedor);

      setListo(true);
    });

    return () => {
      cancelado = true;
      observador?.disconnect();
      mapa?.remove();
      mapaRef.current = null;
      marcadoresRef.current = null;
      setListo(false);
    };
  }, [cartografia]);

  // Elementos de la aplicación sobre el pueblo: activos, necesidades,
  // proyectos. Es lo que convierte el mapa en una herramienta y no en una
  // lámina.
  useEffect(() => {
    const grupo = marcadoresRef.current;
    if (!listo || !grupo) {
      return;
    }

    let cancelado = false;
    import("leaflet").then((L) => {
      if (cancelado) {
        return;
      }
      grupo.clearLayers();
      for (const item of items) {
        const { latitude, longitude } = item.location;
        if (latitude === null || longitude === null) {
          continue;
        }
        const seleccionado = itemKey(item) === selectedItemId;
        const color = markerColors[item.layer_key] ?? COLOR_MARCADOR;
        const marcador = L.circleMarker([latitude, longitude], {
          className: styles.marcador,
          radius: seleccionado ? 9 : 6,
          color,
          fillColor: color,
          fillOpacity: seleccionado ? 0.95 : 0.7,
          weight: seleccionado ? 3 : 2,
        });
        marcador.bindTooltip(item.title, { direction: "top", offset: [0, -6] });
        marcador.on("click", () => seleccionRef.current?.(item));
        marcador.addTo(grupo);
      }
    });

    return () => {
      cancelado = true;
    };
  }, [items, markerColors, selectedItemId, listo]);

  return (
    <div
      aria-label={`Mapa de ${cartografia.municipio}`}
      className={styles.mapa}
      data-detalle={nivel}
      ref={contenedorRef}
      role="application"
    />
  );
}
