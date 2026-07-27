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
  `285037c16462406560a71c738fca14c2402e76f0159d16e9a608974ee5e396dd`.
- Fuente seleccionada cuando la equivalencia es elegible:
  `https://www.ign.es/wms/pnoa-historico`, protocolo `wms_tiles`, JPEG y estilo
  vacío.
- Atribución que debe conservar una revisión humana que permita servicio local:
  `Sistema Cartográfico Nacional · Instituto Geográfico Nacional de España`.

Los tres documentos se verifican por hash en cada carga. Los títulos, resúmenes
y registros de metadatos del perfil también deben coincidir con los dos
GetCapabilities versionados.

## Resultado por capa

`exact` significa aquí «mismo producto PNOA y año, con declaraciones de
cobertura y resolución compatibles en ambos GetCapabilities». No afirma
igualdad píxel a píxel ni que la pirámide local a zoom 15 conserve la resolución
nativa del vuelo. `substitute_degraded` sí se puede entregar localmente, pero su
motivo permanece visible e inmutable y nunca se presenta como equivalente.

| SIUR | IGN | Cobertura/resolución declarada para Castilla y León | Estado | Fuente automática |
| --- | --- | --- | --- | --- |
| `Ortofoto_2023` | `PNOA2023` | IGN 0,25 m; ITACyL añade bordes de máxima actualidad y Valladolid a 0,10 m | `substitute_degraded` | Sí, degradada |
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
| `Ortofoto_2021` | `PNOA2021` | El resumen no incluye Castilla y León | `blocked` | No |

Por tanto, se crean 19 fuentes IGN: seis exactas y trece degradadas. Las cinco
capas anuales 1997–2002 pueden reutilizar el mosaico genérico `SIGPAC`, pero cada
una muestra su año solicitado, la fuente real y que no es un mosaico anual
equivalente. `Ortofoto_2021` no crea candidato ni puede promoverse.

## Perfil local de las 19 fuentes entregables

- límites finitos: oeste `-7.6`, sur `39.9`, este `-1.3`, norte `43.4`;
- zoom `0..15`, máximo 2.000.000 de teselas;
- superteselas WMS de `8 × 8` y salida JPEG;
- comprobación programada cada 24 horas y refresco completo de teselas cada 7
  días según la política común del espejo;
- definición completa ligada a hash: endpoint, capa remota, protocolo, formato,
  límites, zoom, prioridad, perfil operativo y evidencia revisada.

El bootstrap desactiva cualquier fuente automática ITACyL anterior. Las trece
fuentes degradadas quedan como `candidate_substitute_degraded`, con su
clasificación incorporada a la definición hash-bound. La única estrategia
`reviewed_ortho_substitution_blocked` es `Ortofoto_2021`.

## Barreras operativas

1. Descubrimiento genera candidato para seis entradas `exact` y trece
   `substitute_degraded`; no lo genera para 2021.
2. Una autorización humana puede permitir descarga y servicio de una degradada,
   pero no puede cambiar su clasificación a `exact`; cualquier alteración rompe
   la definición y la evidencia esperadas.
3. Los metadatos locales conservan la comparación degradada y rechazan 2021.
4. El API y el árbol de capas muestran el estado y el aviso sin exponer URLs de
   adquisición ni términos internos.
5. Cada una de las 19 fuentes necesita su propia revisión
   `siur-mirror-authorization-v1`, ligada a su `source_id` y
   `source_definition_sha256`, antes del primer acceso de red.

Este cambio no crea ni simula esas 19 revisiones humanas. La evidencia de
licencia incluida en el perfil permite preparar la revisión; no concede por sí
sola permiso operativo. Tras integrar el código hay que aplicar el bootstrap,
revisar y persistir cada autorización ligada a su fuente exacta, y sólo
entonces ejecutar y validar las 19 sincronizaciones iniciales. En este lote una
degradada se sirve bajo la capa SIUR existente con un aviso persistente; no se
crea un alias paralelo adicional.
