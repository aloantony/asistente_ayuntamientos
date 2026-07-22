# Integración completa con SIUR

Actualizado: 2026-07-17.

## Objetivo de producto

El mapa de `asistente-ayuntamientos` debe ser un superconjunto verificable del visor SIUR: debe conservar todo su catálogo, jerarquía, bases, capas, orden, visibilidad, estilos, leyendas, metadatos y operaciones públicas, y añadir las capas y funciones municipales presentes o futuras.

El XML WMC descargado desde SIUR es solo un estado de mapa. No se usa como inventario completo ni como copia de los datos.

## Arquitectura

La integración es centralizada:

```text
SIUR y servicios externos
          ↓ sincronización controlada
catálogo + PostGIS + almacenamiento/caché centrales
          ↓ API, teselas, identificación y descargas
navegador del ayuntamiento
```

El ayuntamiento necesita conexión a `asistente-ayuntamientos`. No se contempla funcionamiento sin Internet, servidor local, PWA offline ni paquetes por municipio. El navegador tampoco debe conectarse directamente a IDECyL o a los demás proveedores: recibirá únicamente rutas de nuestra API y solo los datos necesarios para la vista activa.

## Catálogo de referencia

El dominio `reference_layers` es global y queda separado de:

- `geo_locations`, que representa ubicaciones pequeñas vinculadas al trabajo municipal;
- `ReferenceDatasetVersion`, que versiona un archivo oficial concreto de geografía municipal;
- documentos privados de cada organización.

El catálogo conserva:

- snapshots íntegros e inmutables del bruto y de su definición normalizada,
  cada uno con su hash, fecha y conteos;
- servicios WMS, WFS, WMTS, XYZ, ArcGIS REST o locales;
- nodos jerárquicos `group | layer` con identidad estable ajena al título visible;
- configuración de representación, escalas, CRS, consulta, descarga y procedencia;
- estados `active | degraded | missing | disabled`;
- preferencias de visibilidad y opacidad por organización.

Una entrada que desaparece del origen pasa a `missing`; no se elimina ni pierde
sus preferencias. El consumidor debe indicar explícitamente el proveedor al
consultar el catálogo. El API público no devuelve URL base, URL de capacidades,
URL de licencia, errores del upstream ni opciones internas del proveedor.

El catálogo solo persiste definiciones de adaptadores internos; sus rutas
públicas nunca realizan peticiones a una URL aportada por el usuario. Los
adaptadores que se incorporen después deberán aplicar una lista de hosts por
proveedor, resolver y bloquear redes privadas, locales, reservadas y link-local,
repetir la validación en cada redirección y limitar tiempos, tamaño y tipo de
respuesta. La validación sintáctica al guardar una URL no se tratará como una
barrera SSRF suficiente.

La inmutabilidad se aplica a los payloads y hashes del snapshot. `status`,
`is_current` y `updated_at` son estado explícito de su ciclo de promoción.

## Matriz de paridad

Antes de considerar completa la integración se mantendrá una matriz versionada con una fila por capa y una fila por función del visor. Cada fila tendrá estado, evidencia automatizada, restricciones de licencia y estrategia de entrega:

- `proxy`: render o consulta central del servicio remoto;
- `mirror`: copia central versionada y consultable;
- `blocked`: pendiente de licencia o limitación técnica explícita.

El sincronizador comparará el catálogo vivo con el último snapshot. Altas, cambios, bajas y elementos no resueltos bloquearán una promoción automática hasta ser revisados; nunca se aceptará como “completo” el subconjunto de once capas del WMC.

## Entregas apiladas

1. Capacidad PostGIS junto a pgvector.
2. Catálogo genérico, versionado y preferencias por organización.
3. Adaptador SIUR, fixtures revisados, dry-run y promoción explícita.
4. Espejo PostGIS de todos los vectores técnica y jurídicamente descargables.
5. Proxy/caché central para WMS, WMTS y ráster permitidos.
6. Teselas, leyendas, metadatos, identificación, búsquedas y descargas.
7. Árbol completo en Leaflet, más capas municipales y herramientas del asistente.
8. Detección continua de deriva y pruebas de paridad funcional.

Los documentos PlanPublica se incorporarán solo después de verificar licencia, atribución, retención y límites de descarga. Hasta entonces se conservarán enlaces y metadatos revisados, sin indexación jurídica automática.
