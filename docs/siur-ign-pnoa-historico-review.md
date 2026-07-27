# Revisión de sustitución ITACyL → IGN PNOA Histórico

Estado de la revisión técnica: 2026-07-27.

Esta revisión cubre las 20 capas `Ortofoto_*` del catálogo SIUR cuyo WMS
primario era `https://orto.wms.itacyl.es/WMS`. No presupone que una coincidencia
de año sea una equivalencia cartográfica: se revisan por separado producto,
intervalo temporal, cobertura declarada de Castilla y León y resolución
publicada.

## Evidencia inmutable

- GetCapabilities IGN WMS 1.3.0, `updateSequence=2619`:
  `backend/app/reference_layers/evidence/ign_pnoa_historico/capabilities-20260727.xml`,
  SHA-256
  `1a0fede7e1d1bfd2746656b7b2e731df6376e5c42f55d8f7c70cf3b1b5e9c8e8`.
- GetCapabilities ITACyL WMS 1.3.0:
  `backend/app/reference_layers/evidence/ign_pnoa_historico/itacyl-capabilities-20260727.xml`,
  SHA-256
  `70f38ef5aee1e78cdc2ac64985e804a7b3f38ab37e13bc435c093c69bbac98ff`.
- Perfil revisado de las 20 identidades:
  `backend/app/reference_layers/evidence/ign_pnoa_historico/equivalence-profile-v1.json`,
  SHA-256
  `951082e32627c1744e7f04bb4592a315857c08047c3240cf47187c5ebefd1187`.
- Fuente seleccionada cuando la equivalencia es elegible:
  `https://www.ign.es/wms/pnoa-historico`, protocolo `wms_tiles`, JPEG y estilo
  vacío.
- La atribución no es genérica: se conserva por capa con la fórmula oficial de
  obra derivada. Ejemplos: `Obra derivada de PNOA 2020 CC-BY 4.0 scne.es`,
  `Obra derivada de Orto-SIGPAC 1997-2003 CC-BY 4.0 scne.es`,
  `Obra derivada de Orto-Interministerial 1976-1986 CC-BY 4.0 scne.es` y
  `Obra derivada de Orto-AMS 1956-1957 CC-BY 4.0
  ejercito.defensa.gob.es`.

Los tres documentos se verifican por hash en cada carga. Los títulos, resúmenes
y registros de metadatos del perfil también deben coincidir con los dos
GetCapabilities versionados.

## Resultado por capa

`exact` significa aquí «mismo producto PNOA y año, con declaraciones de
cobertura y resolución compatibles en ambos GetCapabilities». La clasificación
por sí sola no autoriza promoción: la entrega debe conservar además un gate
ejecutable con comparación de máscaras de cobertura, tamaño, resolución,
escala, píxeles representativos y coincidencia entre la muestra IGN y los bytes
locales. `substitute_degraded` también necesita ese gate; su diferencia queda
visible e inmutable y nunca se presenta como equivalente.

| SIUR | IGN | Cobertura/resolución declarada para Castilla y León | Estado | Fuente automática |
| --- | --- | --- | --- | --- |
| `Ortofoto_2023` | `PNOA2023` | IGN 0,25 m; ITACyL añade bordes de máxima actualidad y Valladolid a 0,10 m | `substitute_degraded` | Sí, degradada |
| `Ortofoto_2021` | `PNOA2020` | ITACyL 2021 cubre solo el sector oriental a 0,25 m; IGN 2020 cubre toda CyL con una anualidad distinta | `substitute_degraded` | Sí, degradada |
| `Ortofoto_2020` | `PNOA2020` | Completa, 0,25 m | `exact` | Sí |
| `Ortofoto_2017` | `PNOA2017` | Completa, 0,25 m | `exact` | Sí |
| `Ortofoto_2014` | `PNOA2014` | Completa, 0,50 m | `exact` | Sí |
| `Ortofoto_2011` | `PNOA2011` | IGN norte/sur; ITACyL no declara el cuadrante SE | `substitute_degraded` | Sí, degradada |
| `Ortofoto_2010` | `PNOA2010` | Ambos: NW 0,50 m y SE 0,25 m | `exact` | Sí |
| `Ortofoto_2009` | `PNOA2009` | ITACyL NW/SE; IGN SW/NE | `substitute_degraded` | Sí, degradada |
| `Ortofoto_2008` | `PNOA2008` | ITACyL NE/SW; IGN NW/SE | `substitute_degraded` | Sí, degradada |
| `Ortofoto_2007` | `PNOA2007` | ITACyL SW/NE; IGN SW/norte; falta comparar máscaras | `substitute_degraded` | Sí, degradada |
| `Ortofoto_2006` | `PNOA2006` | Ambos: SE 0,25 m y NW 0,50 m | `exact` | Sí |
| `Ortofoto_2005` | `PNOA2005` | Ambos: NE 0,25 m y SW 0,50 m | `exact` | Sí |
| `Ortofoto_2004` | `PNOA2004` | ITACyL NW 0,25/SE 0,50 m; IGN declara CyL a 0,50 m | `substitute_degraded` | Sí, degradada |
| `Ortofoto_2002` | `SIGPAC` | ITACyL anual a 0,50 m; IGN varía entre 1997–2003 | `substitute_degraded` | Sí, degradada |
| `Ortofoto_2001` | `SIGPAC` | ITACyL anual a 0,60 m; IGN varía entre 1997–2003 | `substitute_degraded` | Sí, degradada |
| `Ortofoto_2000` | `SIGPAC` | ITACyL anual/proyectos a 0,70 m; IGN varía entre 1997–2003 | `substitute_degraded` | Sí, degradada |
| `Ortofoto_1999` | `SIGPAC` | ITACyL anual/proyectos a 0,70 m; IGN varía entre 1997–2003 | `substitute_degraded` | Sí, degradada |
| `Ortofoto_1997` | `SIGPAC` | ITACyL SIG oleícola a 1 m; IGN es un mosaico 1997–2003 | `substitute_degraded` | Sí, degradada |
| `Ortofoto_1973-83` | `Interministerial_1973-1986` | Mismo vuelo base, distinta ortorrectificación/intervalo declarado | `substitute_degraded` | Sí, degradada |
| `Ortofoto_1956` | `AMS_1956-1957` | ITACyL 0,40 m; IGN 0,50–1 m y sólo parte de España | `substitute_degraded` | Sí, degradada |

Por tanto, se crean 20 fuentes IGN: seis exactas y catorce degradadas. Las cinco
capas anuales 1997–2002 pueden reutilizar el mosaico genérico `SIGPAC`, pero cada
una muestra su año solicitado, la fuente real y que no es un mosaico anual
equivalente. `Ortofoto_2021` reutiliza `PNOA2020` solo como sustitución
degradada explícita: no se afirma que represente la misma anualidad ni la misma
máscara.

## Perfil local de las 20 fuentes entregables

- límites finitos: oeste `-7.6`, sur `39.9`, este `-1.3`, norte `43.4`;
- zoom `0..15`, máximo 2.000.000 de teselas;
- superteselas WMS de `8 × 8` y salida JPEG;
- comprobación programada cada 24 horas y refresco completo de teselas cada 7
  días según la política común del espejo;
- definición completa ligada a hash: endpoint, capa remota, protocolo, formato,
  límites, zoom, prioridad, perfil operativo y evidencia revisada.

El bootstrap desactiva cualquier fuente automática ITACyL anterior. Las catorce
fuentes degradadas quedan como `candidate_substitute_degraded`, con su
clasificación incorporada a la definición hash-bound.

## Barreras operativas

1. Descubrimiento genera candidato para seis entradas `exact` y catorce
   `substitute_degraded`.
2. Una autorización humana puede permitir descarga y servicio de una degradada,
   pero no puede cambiar su clasificación a `exact`; cualquier alteración rompe
   la definición y la evidencia esperadas.
3. Antes de descargar teselas y de promover se comparan las capacidades vivas
   IGN e ITACyL con la semántica versionada: identidad, título, resumen, CRS,
   formatos, estilos, extensión, atribución, restricciones y metadatos. Un
   cambio semántico exige revisión y falla cerrado.
4. La versión candidata puede prepararse, pero no promoverse sin el gate de
   paridad persistido y ligado por hash al contenido local.
5. Servir, reactivar o revertir vuelve a validar la fuente congelada, la
   clasificación y el gate. Cualquier entrega histórica de `Ortofoto_2021`
   ligada al emparejamiento rechazado con `PNOA2021`, y cualquier versión sin
   la evidencia nueva, quedan cercadas.
6. Los metadatos locales conservan clasificación, atribución, hash de los bytes
   activos y hash de paridad.
7. El API y el árbol de capas distinguen una fuente `candidate` de una
   `active_delivery`; si hay bytes activos nunca los describen con la candidata
   actual.
8. Cada una de las 20 fuentes necesita su propia revisión
   `siur-mirror-authorization-v1`, ligada a su `source_id` y
   `source_definition_sha256`, antes del primer acceso de red.

## Qué debe revisar Antonio

Para cada una de las 20 fuentes, Antonio debe recibir el documento exacto
`siur-mirror-authorization-v1` y comprobar:

1. que `source_id`, `source_definition_sha256`, capa IGN y capa SIUR son las que
   figuran en esta matriz;
2. que la fórmula de atribución corresponde al producto y fecha concretos y se
   mostrará en mapa, metadatos y servicio;
3. si las condiciones oficiales permiten, para el uso real del despliegue,
   las tres decisiones separadas: descarga masiva de teselas, conservación
   estable en nuestros servidores y re-servicio local a los usuarios;
4. que los permisos `metadata_probe`, `dataset_download`, `bulk_tile_seed`,
   `local_storage` y `local_service` reflejan literalmente su decisión, sin
   ampliar un «sí» parcial;
5. que las catorce sustituciones degradadas siguen rotuladas como degradadas
   en selector, panel y metadatos, incluida `Ortofoto_2021 → PNOA2020`;
6. que los hashes `review_sha256` y `document_sha256` del dry-run son los mismos
   que se aplicarán.

Las comprobaciones técnicas de capacidades y píxeles evitan datos erróneos,
pero no deciden por Antonio los permisos jurídicos de descarga, conservación o
re-servicio. El sistema no genera ni aprueba automáticamente esas revisiones.

Este cambio no crea ni simula esas 19 revisiones humanas. La evidencia de
licencia incluida en el perfil permite preparar la revisión; no concede por sí
sola permiso operativo. Tras integrar el código hay que aplicar el bootstrap,
revisar y persistir cada autorización ligada a su fuente exacta, y sólo
entonces ejecutar y validar las 19 sincronizaciones iniciales. En este lote una
degradada se sirve bajo la capa SIUR existente con un aviso persistente; no se
crea un alias paralelo adicional.
