# SIGPAC: captura segura de metadatos ZIP

SIGPAC 2022 y 2024 siguen clasificados como `restricted`. Los índices oficiales
versionados prueban las dos rutas HTTPS y los nueve ZIP provinciales, pero no
prueban sus miembros internos, la capa a importar, los estilos ni el contenido
de la licencia. Por eso esta fase no crea `decision-manifest-v5.json` ni
convierte las dos capas en candidatas.

`app.reference_layers.zip_metadata_probe` permite obtener la evidencia que
falta sin descargar los aproximadamente 19 GiB de datos. Para cada ZIP realiza
un `HEAD` y lecturas `Range` exactas de:

1. EOCD y, cuando corresponde, localizador/EOCD ZIP64.
2. Directorio central ZIP.

Cada lectura usa HTTPS con DNS público e IP fijada, origen exacto, TLS por
hostname, `Accept-Encoding: identity`, cero redirecciones, `If-Match` con ETag
fuerte, `206` y `Content-Range` exactos. Se rechazan rangos ignorados o
truncados, ZIP multidisco, miembros cifrados, ZIP bombs, rutas inseguras,
duplicados y cambios de validador.

## Gate de revisión

El operador debe disponer de un `ReferenceLayerSource` deshabilitado,
no primario y `sync_strategy=manual`, cuya definición sea exactamente la
devuelta por `metadata_probe_source_definition(plan)`. La revisión persistida
debe:

- estar aprobada;
- permitir únicamente `metadata_probe`;
- dejar a `false` `dataset_download`, `local_storage`, `local_service` y
  `bulk_tile_seed`;
- permitir únicamente el origen `https://ftp.itacyl.es`.

La revisión se valida antes de la primera petición y después de inspeccionar
los nueve recursos. La sonda no usa el CAS, no crea `ReferenceSyncRun`, no crea
versiones y no promociona entregas.

## Flujo operador

El dry-run no usa la red ni escribe el archivo:

```bash
python -m app.reference_layers.zip_metadata_probe \
  --source-id SOURCE_ID \
  --root-index /ruta/review-sigpac-2024-root.html \
  --province-index /ruta/review-sigpac-2024-provinces.html \
  --output /ruta/sigpac-2024-probe.json
```

La salida incluye el hash del plan, la definición de fuente, la revisión y las
nueve URL. Tras revisar esos valores, la captura exige repetir el hash:

```bash
python -m app.reference_layers.zip_metadata_probe \
  --source-id SOURCE_ID \
  --root-index /ruta/review-sigpac-2024-root.html \
  --province-index /ruta/review-sigpac-2024-provinces.html \
  --output /ruta/sigpac-2024-probe.json \
  --apply \
  --expected-plan-sha256 HASH_DEL_DRY_RUN
```

La ruta de salida se crea de forma atómica y nunca se reemplaza. La salida
informa del SHA-256 semántico del manifiesto y del SHA-256/longitud del archivo.

## Evidencia aún necesaria para v5

Antes de versionar v5 deben revisarse y fijarse, para los 18 ZIP:

- ETag fuerte, `Last-Modified`, `Content-Length` y hash del directorio central;
- inventario canónico completo (nombre, CRC32, tamaños, método y offset);
- miembro SHP/GPKG exacto e `input_layer` de cada provincia;
- conjunto coherente de componentes Shapefile y esquema/CRS esperado;
- presencia y contenido hash-bound de licencia, o evidencia oficial externa
  equivalente;
- estilos oficiales completos o una adaptación local revisada para todas las
  identidades de estilo del catálogo;
- límites por archivo y agregados derivados de los tamaños observados.

Solo un sucesor versionado que valide esa evidencia podrá definir
`download_resources`, límites de ingestión y estilos. Incluso entonces,
descarga, conservación y servicio local seguirán necesitando una nueva revisión
humana con permisos explícitos; la revisión metadata-only no los amplía.
