# Espejo cartográfico local de SIUR

## Objetivo y criterio de completitud

La integración se considera completa cuando cada capa hoja del catálogo SIUR
vigente tiene una estrategia de adquisición explícita, un producto local
persistente y versionado en nuestros servidores y una entrega que no necesita
contactar con el proveedor durante una petición del visor. El producto
persistente puede ser el original o una derivación revisada y reproducible
cuando conservar el bruto no sea necesario ni esté autorizado.

El catálogo se comprueba al menos una vez al día. Una comprobación sin cambios
no vuelve a descargar ni publicar el dataset. Una actualización se descarga a
`staging`, se valida, se publica con un identificador inmutable y solo entonces
se promociona en una transacción corta. Si cualquier paso falla, se sigue
sirviendo la versión anterior. El rollback es una nueva promoción auditada.

No se acepta como estado final ninguna capa que caiga silenciosamente al proxy
WMS remoto. La matriz de cobertura debe clasificar las 227 capas actuales y
cualquier alta futura en una de estas estrategias:

1. `vector`: original local o derivado revisado e importación a una tabla
   PostGIS versionada.
2. `raster`: original local y GeoTIFF local optimizado con pirámides.
3. `tiles`: pirámide finita local cuando no existe un dataset descargable.
4. `composition`: composición local de datasets o capas ya versionados.
5. `blocked`: imposibilidad concreta y visible, con causa técnica o de
   autorización; nunca se presenta como disponible.

`blocked` permite describir fielmente una limitación externa, pero no satisface
por sí mismo la cobertura funcional. Una capa solo pasa a disponible cuando una
versión local ha sido validada y promocionada.

## Qué se guarda y qué no se precalcula

Se conservan localmente:

- los bytes originales obtenidos del proveedor, con hash y procedencia, salvo
  una transformación revisada que exija descartarlos tras la derivación;
- la salida normalizada que consume el renderizador;
- estilos SLD, símbolos, metadatos y manifiestos;
- la versión activa y al menos una versión anterior recuperable;
- una pirámide de teselas cuando las imágenes son el único producto disponible;
- la caché persistente generada bajo demanda por GeoWebCache.

Una excepción de este tipo debe estar declarada por la fuente y por su revisión
de autorización. Se conserva como evidencia la URL exacta, hash, tamaño,
validadores HTTP, campos seleccionados, máscara, algoritmo y hash del resultado.
El bruto solo puede existir en almacenamiento transitorio privado y debe
eliminarse al cerrar la derivación. Las cuadrículas Eurostat usan esta excepción
para conservar únicamente geometría e identificadores de las celdas de Castilla
y León, sin retener los atributos de población del GeoPackage de entrada.

No se generan por adelantado todas las combinaciones de capa, estilo, zoom y
tesela cuando existe el dataset vectorial o ráster. GeoServer las renderiza
localmente y GeoWebCache conserva las que realmente se usan. Esto no reduce la
independencia frente al proveedor: los datos necesarios siguen estando en el
servidor propio.

Para un WMS, WMTS o XYZ sin datos fuente alternativos sí hay que fijar una
extensión, matriz y zoom máximo finitos. Esa pirámide constituye el dataset
local de la capa y su tamaño debe entrar en el cálculo de capacidad antes de
habilitar la sincronización.

## Arquitectura

```text
                         plano de ingesta
 fuentes oficiales -> descubrimiento -> descarga content-addressed
                                |                    |
                                v                    v
                       validación/normalización   original inmutable
                                |
                      +---------+----------+
                      |                    |
                 PostGIS/GeoTIFF       archivo de tiles
                      |                    |
                      +---------+----------+
                                v
                    publicación versionada y smoke test
                                |
                         promoción atómica

                         plano de servicio
 navegador -> FastAPI autenticado -> GeoServer/GeoWebCache -> datos locales
                                  `-> servidor de archivo de tiles local
```

El plano de servicio no necesita salida a Internet. FastAPI mantiene las rutas
opacas actuales de teselas, leyenda e identificación; ni el navegador ni sus
parámetros pueden elegir una URL o un nombre interno del renderizador.

## Descubrimiento de fuente

La URL usada para visualizar una capa no determina el modo de descarga. El
descubridor registra candidatos y selecciona uno primario en este orden:

1. descarga oficial versionada o feed ATOM;
2. OGC API Features;
3. WFS para vectores;
4. WCS o descarga de cobertura para ráster;
5. ArcGIS REST con paginación o exportación;
6. WMTS/XYZ;
7. WMS como pirámide de imagen finita.

Para los espacios GeoServer de IDECyL se prueba el servicio de datos del mismo
espacio y se exige que el nombre remoto aparezca en sus capacidades. No se
deduce disponibilidad únicamente sustituyendo texto en una URL. Para ArcGIS se
resuelve el `MapServer` original y se valida que la subcapa permita `query` o
descarga. Los demás proveedores se describen mediante adaptadores o manifiestos
revisables, nunca mediante hosts suministrados por el navegador.

Cada fuente incluye el intervalo de comprobación, su estrategia condicional,
límites de descarga, paginación, CRS esperado, bounds y criterios mínimos de
calidad. La comprobación diaria usa, cuando sean fiables, `ETag`,
`Last-Modified`, versión de capabilities o un manifiesto normalizado. Se fuerza
periódicamente una comprobación completa para detectar servidores que no
mantienen correctamente esos metadatos.

## Almacenamiento y ciclo de vida

Los originales retenidos y los derivados de fichero usan claves
content-addressed basadas en SHA-256. Una descarga se escribe primero en una
ruta temporal privada, con límites de tiempo y tamaño; después se verifica el
hash y, según la política revisada de la fuente, se mueve atómicamente a su clave
definitiva o se transforma y descarta tras registrar su observación exacta.
PostgreSQL solo guarda el inventario y las relaciones, no los cuerpos
cartográficos grandes.

Las tablas vectoriales y nombres de capa internos incorporan el identificador
de versión. Nunca se trunca ni sobrescribe la tabla activa. Los GeoTIFF y
archivos de teselas siguen la misma regla. La recolección de versiones antiguas
se ejecuta aparte de la promoción y nunca elimina la activa ni la última versión
recuperable.

La caché Redis continúa siendo efímera y pequeña. GeoWebCache tiene su propio
volumen y cuota persistentes. Perder cualquiera de esas cachés puede degradar el
rendimiento, pero no la disponibilidad ni los datos locales publicados.

## Validación antes de promoción

Todas las fuentes verifican como mínimo:

- hash, tamaño, tipo real y ausencia de contenido HTML/XML de error inesperado;
- CRS y bounds compatibles con el manifiesto;
- conteos y esquema dentro de umbrales explicables respecto a la versión activa;
- geometrías válidas o una reparación explícita y cuantificada;
- resolución, bandas, `nodata` y overviews en ráster;
- cobertura, formato y porcentaje de teselas válidas en archivos de imagen;
- publicación local, una tesela PNG y, cuando corresponda, leyenda e identify;
- referencias de estilo resueltas exclusivamente a recursos locales.

Una variación por encima de los umbrales no se promociona automáticamente. Se
registra como rechazada y la versión vigente permanece intacta.

## Estilos y paridad

Los nombres de estilo que contiene hoy el catálogo no bastan para reproducir la
cartografía. Para cada estilo se conserva el SLD original cuando el proveedor lo
expone, se rechazan entidades externas, se localizan símbolos y se publica un
nombre interno inmutable. La matriz distingue:

- `exact`: SLD y recursos reproducidos;
- `adapted`: estilo local versionado y revisado;
- `baked`: simbología incorporada en la pirámide de imágenes;
- `missing`: todavía no renderizable y, por tanto, no disponible.

## Operación y recuperación

Un scheduler encola las fuentes vencidas una vez al día y una cola geográfica
separada ejecuta las descargas. Cada fuente admite un único run abierto. Los
workers usan lease con token de fencing y no mantienen locks de base de datos
durante red, GDAL ni publicación. La promoción relee la generación activa bajo
lock y falla si otro proceso se adelantó.

Las métricas mínimas son: última comprobación, fecha del dato, versión activa,
bytes almacenados, duración, siguiente comprobación, resultado de validación y
error de la actualización más reciente. El frontend diferencia “local
disponible”, “sincronizando”, “sirviendo versión anterior” y “bloqueada”, sin
confundir un error de actualización con la indisponibilidad del mapa vigente.

La recuperación documentada parte de PostgreSQL, los artefactos y el directorio
de GeoServer. Este último incluye toda la configuración persistente de
GeoWebCache; únicamente su volumen separado de teselas es reconstruible, igual
que Redis. Una copia de seguridad que solo incluya PostgreSQL no es suficiente.

## Prueba integral de independencia

La aceptación final debe:

1. sincronizar fuentes vectoriales, ráster y de solo teselas;
2. cortar la salida a Internet del backend de servicio y GeoServer;
3. vaciar Redis y reiniciar los servicios;
4. cargar el mapa, estilos y leyendas e identificar una capa local;
5. simular una descarga corrupta y confirmar que se conserva la versión activa;
6. promocionar una segunda versión y ejecutar un rollback;
7. comprobar que el inventario completo no contiene entregas disponibles en
   modo proxy.

Los fondos base forman parte de esta prueba. No se considera independiente un
visor que aún cargue OSM u OpenTopoMap directamente desde el navegador.

### Corte de red reproducible

`docker-compose.siur-offline.yml` cambia temporalmente todos los procesos de
servicio, persistencia y sincronización a un único espacio de red conectado
solo a una red Docker `internal`. Los puertos siguen publicados exclusivamente
en `127.0.0.1`, de modo que el navegador y el operador conservan acceso local,
pero backend, GeoServer, workers, PostgreSQL y Redis no tienen salida a
Internet. Los nombres históricos `postgres`, `redis` y `geoserver` resuelven al
mismo espacio de red para que la configuración persistida de GeoServer siga
siendo válida.

La topología se activa únicamente después de terminar y comprobar todas las
sincronizaciones:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.siur-offline.yml \
  up -d --force-recreate
```

El cambio recrea Redis sin datos persistentes y por tanto cubre también el
reinicio con caché vacía. Antes de aceptar el resultado se debe demostrar desde
el contenedor backend que una conexión TCP pública falla y que PostgreSQL,
Redis y GeoServer siguen accesibles por loopback; después se recorren mapa,
estilos, leyenda e identify en el navegador.

Para recuperar la operación periódica normal se recrea el proyecto solo con el
archivo principal (sin borrar volúmenes):

```bash
docker compose -f docker-compose.yml up -d --force-recreate --remove-orphans
```

No se usa `down -v`: PostgreSQL, artefactos, GeoServer y GeoWebCache deben
conservarse durante ambos cambios de topología.
