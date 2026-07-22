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
- estilos normalizados por capa, incluido el predeterminado y la disponibilidad
  de leyenda, sin entregar al navegador la URL remota;
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

## Fuentes y evidencias SIUR

La fuente autoritativa del inventario es
`assets/settings/settings.json`. Su adaptador funciona primero en `dry-run`,
conserva el JSON bruto y su SHA-256, contabiliza todos los nodos no resueltos y
solo permite promover una definición con paridad completa y un hash aprobado.
La futura descarga programada, sus redirecciones y límites se ejecutará
exclusivamente en backend contra host y ruta fijos; el comando de esta entrega
solo lee archivos locales revisados.

El WMC aportado se conserva como fixture de evidencia independiente. El parser
acepta únicamente WMC 1.1.0, rechaza DTD y entidades, limita tamaño y número de
elementos y valida las URL contra el host SIUR. En el archivo recibido comprueba
11 capas visibles y consultables, 3 servicios WMS, 28 estilos, 11 estilos
seleccionados y 10 referencias de metadatos en `EPSG:25830`. Esta sonda puede
bloquear la promoción si el catálogo completo pierde uno de esos elementos,
pero ignora correctamente todas las capas adicionales del catálogo: nunca se
aplica el WMC como inventario. En esta entrega la comparación automatizada
cubre proveedor, protocolo/endpoint/versión WMS, identidad de capa, presencia
de estilos y selección predeterminada. La paridad visual y funcional de CRS,
escalas, opacidad, consulta, leyenda y metadatos se mantiene como filas
separadas de la matriz y no se da por satisfecha solo por superar esta sonda.

El flujo de operador empieza siempre en solo lectura:

```bash
python -m app.reference_layers.siur_sync \
  --settings /ruta/settings.json \
  --wmc /ruta/context.xml
```

El informe enumera conteos, identidades de todas las capas, hashes, campos no
resueltos y diferencias. Para el siguiente dry-run se indican juntos los cuatro
conteos revisados, un manifiesto JSON con todas las identidades, el SHA-256
exacto del JSON y el del WMC. Ese informe produce además los hashes de la
definición normalizada y del plan contra el estado actual de la base de datos.
Solo cuando también se aprueban esos dos hashes puede repetirse el mismo comando
con `--apply`. El plan expone una huella `base_state_sha256` del snapshot,
servicios, capas y estilos observados, y su hash aprobado incluye esa huella.
Al aplicar, el backend serializa por proveedor, recarga el estado y reconstruye
el plan dentro de la misma transacción; una promoción concurrente invalida la
aprobación aunque ambas operaciones afecten a las mismas identidades. Cambiar
los bytes, el resultado del adaptador o el estado base invalida la aprobación.
El comando no acepta URL ni descarga nada, por lo que esta fase tampoco
introduce un proxy abierto.

El WMC puede completar estilos, leyendas y versión WMS únicamente sobre capas
que ya existan y coincidan de forma unívoca en el catálogo completo. Nunca crea
una capa ausente en `settings.json`; esa ausencia sigue siendo un bloqueo.

## Evidencia persistente de entrega WMS

La autoridad de entrega se separa del catálogo mutable en tres registros
inmutables y apend-only:

1. Una instantánea técnica GetCapabilities conserva el XML exacto acotado, su
   SHA-256 bruto, el SHA-256 de una normalización versionada, la versión WMS,
   los endpoints GET verificados de GetMap, GetLegendGraphic y GetFeatureInfo,
   sus formatos y un manifiesto por nombre remoto con CRS literales, estado
   `queryable` y estilos (incluido `""` para
   el estilo predeterminado implícito).
2. Una revisión humana conserva el documento JSON exacto revisado, su hash, el
   hash de la decisión normalizada, revisor, fecha, información y términos de
   licencia, decisión, el hash de la revisión humana anterior que sustituye y
   permisos independientes `allow_proxy` y `allow_cache`.
3. Una atestación enlaza un snapshot y `definition_sha256` exactos del catálogo
   con una instantánea de capacidades y una revisión. Las atestaciones forman
   una cadena no bifurcable, con secuencia y hash de la anterior, y registran
   una activación técnica o una revocación legal explícita. Su hash cubre la
   transición, las identidades y todos los hashes de evidencia vinculados.

Las tres tablas rechazan `UPDATE` y `DELETE` en PostgreSQL. Insertar evidencia
técnica que no coincide con el catálogo no cambia por sí solo la autoridad de
entrega. Solo el extremo válido de la cadena es actual; esto permite reactivar
de forma explícita una instantánea técnica anterior sin reutilizar su época de
caché. Una revisión legal distinta debe enlazar la revisión actual y tener una
fecha posterior; la cadena de revisiones también impide dos revisiones génesis
o dos sucesoras del mismo documento. Si una revisión nueva sustituye a la
anterior pero la evidencia técnica aportada todavía no coincide con el
catálogo, se añade un extremo de revocación: los permisos anteriores de proxy y
caché dejan de ser efectivos inmediatamente. La misma revisión puede activarse
después con una instantánea técnica válida. Así no se puede reproducir una
aprobación antigua ni conservar su permiso de caché por un fallo técnico. Un
cambio de catálogo invalida automáticamente el extremo anterior porque el
runtime exige el ID, el contenido normalizado y el `definition_sha256` del
snapshot vigente.

El importador solo lee archivos locales y empieza en dry-run:

```bash
python -m app.reference_layers.siur_delivery_import \
  --capabilities /ruta/GetCapabilities.xml \
  --license-review /ruta/license-review.json
```

El informe produce los hashes bruto, normalizado, de revisión, atestación y
plan. Para `--apply` se deben repetir exactamente los cinco mediante las
opciones `--approved-*-sha256`. El comando vuelve a bloquear servicio y
snapshot, recalcula el plan y confirma una única transacción. No acepta URLs,
no descarga documentos y no escribe `ReferenceService.license_status`,
`ReferenceService.capabilities_sha256` ni
`ReferenceLayer.supported_crs_json`.

Una revisión `restricted`, `rejected` o aprobada sin permiso de proxy genera
una atestación de revocación que pasa a ser el extremo actual y corta la
entrega antes de Redis o de la red. Nunca se aprueba automáticamente la
licencia real de SIUR: una aprobación aplicable exige un documento de revisión
humana auténtico y hash-aprobado; los fixtures automatizados son sintéticos y
lo declaran.

## Matriz de paridad

Antes de considerar completa la integración se mantendrá una matriz versionada con una fila por capa y una fila por función del visor. Cada fila tendrá estado, evidencia automatizada, restricciones de licencia y estrategia de entrega:

- `proxy`: render o consulta central del servicio remoto;
- `mirror`: copia central versionada y consultable;
- `blocked`: pendiente de licencia o limitación técnica explícita.

El sincronizador comparará el catálogo vivo con el último snapshot. Altas, cambios, bajas y elementos no resueltos bloquearán una promoción automática hasta ser revisados; nunca se aceptará como “completo” el subconjunto de once capas del WMC.

## Entrega cartográfica central

La primera entrega WMS expone únicamente tres operaciones tipadas por
organización e identificador interno de capa:

- teselas PNG `z/x/y` de 256 píxeles en `EPSG:3857`;
- leyenda PNG del estilo autorizado;
- identificación en un píxel, limitada a una colección GeoJSON pequeña.

El navegador no puede indicar una URL, un host, un nombre WMS, un `bbox`, un
CRS, un formato ni parámetros OGC libres. El backend obtiene servicio, capa y
estilo por IDs, comprueba `map.view`, pertenencia a la organización y exige la
atestación actual exacta del snapshot vigente. La entrega vuelve a verificar
el endpoint y versión del servicio, la presencia exacta de capa y estilo (el
navegador solo aporta `style_id`), `image/png` y el literal `EPSG:3857` contra
GetCapabilities. La leyenda exige su propio endpoint GetLegendGraphic y PNG
atestados. Identify exige además capa consultable, endpoint GetFeatureInfo
verificado y soporte literal de `application/json`. Los campos de inventario
que reescribe el sincronizador no actúan como autoridad legal o técnica.

En esta primera política el único origen permitido es HTTPS en
`idecyl.jcyl.es:443` y las rutas WMS/OWS de sus espacios GeoServer. Cada fallo
de caché vuelve a resolver DNS, rechaza la respuesta completa si contiene una
IP no pública y conecta al IP validado conservando TLS/SNI para el host. No se
siguen redirecciones, no se acepta compresión y se limitan tiempo, concurrencia,
tipo y bytes. Los errores remotos se convierten en un `502` genérico, sin
devolver cuerpos, cabeceras, URL ni mensajes del proveedor.

La ruta rechaza teselas fuera del intervalo de zoom o de la extensión
geográfica declarada por la capa. Una respuesta PNG solo se acepta si su
cabecera e integridad IHDR son válidas; las teselas deben medir exactamente
256×256 y las leyendas tienen límites de dimensiones y píxeles.

Teselas y leyendas usan Redis como caché central solo si la política del
servicio es `on_demand` o `mirror` y la revisión humana vigente permite caché.
La clave es un SHA-256 opaco que incluye el hash de atestación e identificadores
internos; no contiene la URL ni los nombres remotos. Se conserva una ventana
obsoleta acotada para poder servir la última imagen válida cuando SIUR falle
temporalmente. Las respuestas llevan ETag, caché privada y `nosniff`. La
identificación no se almacena. El espacio WMS mantiene un presupuesto atómico
propio de 128 MiB, contabiliza el payload más 512 bytes conservadores por
entrada, limita además el namespace a 50.000 entradas y desaloja por LRU al
superar cualquiera de los dos límites. El GET, toque LRU y saneamiento de
miembros expirados son una única operación Lua. Así una secuencia de
coordenadas distintas no puede consumir sin límite la memoria reservada para
colas y estado de la aplicación.

Esto no es un modo sin Internet municipal: el ayuntamiento sigue necesitando
conexión con nuestra aplicación. La caché evita que el navegador dependa de una
segunda conexión directa con SIUR y reduce el impacto de una caída puntual del
proveedor central.

## Entregas apiladas

1. Capacidad PostGIS junto a pgvector.
2. Catálogo genérico, versionado y preferencias por organización.
3. Adaptador SIUR, fixtures revisados, dry-run y promoción explícita.
4. Proxy/caché central WMS para teselas, leyendas e identificación permitidas.
5. Espejo PostGIS de todos los vectores técnica y jurídicamente descargables.
6. WMTS, metadatos, búsquedas y descargas.
7. Árbol completo en Leaflet, más capas municipales y herramientas del asistente.
8. Detección continua de deriva y pruebas de paridad funcional.

Los documentos PlanPublica se incorporarán solo después de verificar licencia, atribución, retención y límites de descarga. Hasta entonces se conservarán enlaces y metadatos revisados, sin indexación jurídica automática.
