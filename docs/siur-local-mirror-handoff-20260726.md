# Handoff del espejo local SIUR — 2026-07-26

Checkpoint solicitado antes de agotar el límite de uso. La entrega **no está
terminada ni lista para `main`**. El trabajo aceptado está versionado en la rama
de integración y los dos cierres que seguían en paralelo están versionados en
worktrees independientes, limpios y con sus sesiones en `paused`.

No se ha adquirido el lease del runtime, no se ha migrado la base de desarrollo,
no se ha reconstruido el stack compartido y no se ha iniciado la descarga real
de las 227 capas. Ninguna de las ramas de este checkpoint se ha enviado al
remoto.

## Punto de reanudación principal

- Worktree:
  `/home/dev/proyectos/asistente_ayuntamientos-worktrees/siur-local-mirror`
- Rama: `codex/siur-local-mirror-20260722`
- Último commit de producto integrado: `441c226`
  (`fix: reject implausible reference feature growth`)
- Primer commit de este handoff actualizado: `20419bc`
- Comparación: `origin/main`
- Para obtener la divergencia exacta al retomar:
  `git rev-list --left-right --count origin/main...HEAD`
- Sesión/owner: `root-siur-local-mirror-20260722`
- Estado Git esperado al cerrar: limpio
- Runtime compartido: sin lease
- Base de desarrollo observada, sin modificar: Alembic `20260722_0034`
- Estado de datos observado, sin modificar: 227 capas hoja y 227 entregas en
  modo `proxy`

El objetivo pendiente sigue siendo mantener en nuestros servidores artefactos
locales, inmutables y versionados para todas las capas SIUR; servirlos sin
dependencias remotas; comprobar actualizaciones; validar, promover y revertir
versiones con evidencia; y probar el flujo completo desde el navegador.

## Trabajo integrado desde el checkpoint anterior

La rama ya contenía almacenamiento content-addressed, adquisición con
reanudación, ingesta vectorial/ráster, publicación GeoServer, MBTiles, estilos
SLD, cobertura finita, lifecycle durable y el frontend de capas de referencia.
Desde el antiguo handoff se integraron además:

- `b9be54c`: fuentes oficiales WMS preferentes para PNOA, IGN Base y MTN,
  WMTS como fallback, equivalencias versionadas, preflight estratificado y
  controles de capacidad.
- `f22a193`: validación de tipos de banda, nodata, resolución y pirámides de
  overviews; un COG grande sin overviews no puede promocionarse.
- `cc80ee2` + `a26810b`: continuidad entre snapshots, rollback histórico,
  estado `serving_previous` y cierre ante evidencia manipulada.
- `e4be78f`: la imagen final comprueba en build que existen
  `gdalinfo`, `gdal_translate` y `ogr2ogr`.
- `f242410` + `5415936`: worker y scheduler durables, bootstrap, reclamación
  de runs interrumpidos, preservación de ETag/manifiesto/estadísticas y
  muestreo periódico de píxeles de tiles.
- `4146eb1`: proxy cartográfico remoto desactivado por defecto y opt-in
  explícito; catálogo y rutas fallan antes de red si no existe copia local.
- `07e46d5`: gate prepromoción de tipo, CRS, esquema, bounds, semántica ráster
  y caída masiva de entidades.
- `441c226`: gate adicional ante crecimientos de entidades inverosímiles,
  con umbral y evidencia persistida.

## Pruebas reales acumuladas

- Suite backend completa después de la integración WMS/capacidad:
  `1329 passed, 2 warnings in 356.81s`.
- Suite combinada de referencia después de integrar orquestador, continuidad,
  proxy y gates prepromoción: `408 passed in 22.95s`.
- Regresión focal del último gate de crecimiento junto al orquestador:
  `16 passed`.
- Frontend Vitest: `11 passed`.
- Frontend typecheck: correcto.
- Frontend lint: 0 errores y 11 avisos preexistentes.
- Frontend build de producción: correcto, 23 páginas.

La suite backend completa debe repetirse cuando se incorporen los dos cierres
paralelos descritos abajo. La prueba real de GDAL debe ejecutarse después de
reconstruir la imagen: el contenedor compartido actual procede de una rama
anterior y no contiene aún los binarios que sí instala el Dockerfile integrado.

## Migración hermana ya aplicada en desarrollo

`20260722_0034` no es corrupción de Alembic. Es una migración inmutable de la
funcionalidad de accesos directos:

- archivo original:
  `20260722_0034_add_user_sidebar_shortcuts.py`;
- `down_revision`: `20260717_0033`;
- SHA-256:
  `4d29abea4c8583d4e9225a3f02dc2c7e2f2d132d5dee4a6a04cbb99f8023d346`;
- añade `users.sidebar_shortcut_ids`.

La rama SIUR tiene la hermana `20260717_0034` y después
`20260723_0035 -> 0036 -> 0037`. Nunca se debe borrar, renombrar ni reescribir
la revisión ya aplicada. El cierre correcto es copiarla byte a byte y añadir
una revisión de merge pura con ambas cabezas como padres.

## Cierres paralelos reservados

### Watcher durable del catálogo y merge de migraciones

- Worktree:
  `/home/dev/proyectos/asistente_ayuntamientos-worktrees/catalog-update-watcher-20260726`
- Rama: `codex/catalog-update-watcher-20260726-20260726`
- Sesión: `catalog-update-watcher-20260726`, estado `paused`
- Base original: `b9be54c`
- Estado Git: limpio
- Commits, en orden:
  - `7a7b912` (`chore: include sidebar migration dependency`);
  - `ff610f3` (`feat: watch SIUR catalog updates safely`);
  - `7929abe` (`fix: keep SIUR download outside database transaction`).

`7a7b912` copia byte a byte la migración hermana y la porción exacta del modelo
`User`; debe incluirse en esta rama de integración porque todavía no contiene
la funcionalidad sidebar. Si al retomar ya se hubiera integrado por otra vía,
revisar y omitir únicamente ese commit para no duplicarla.

`ff610f3` añade el merge puro `20260726_0038`, con padres
`("20260723_0037", "20260722_0034")`, y el watcher en `20260726_0039`.
Implementa descarga condicional fijada, hash canónico, versiones observadas
inmutables, resultados `unchanged`, `update_available` o `error`, idempotencia
y staging sin autoaplicar cambios.

La revisión encontró que la primera versión mantenía un lock transaccional
durante la red. `7929abe` lo corrige: preflight DB corto, descarga y parseo con
`db.in_transaction() == False`, y lock transaccional únicamente para revalidar
y persistir. Una regresión demuestra que otra conexión puede adquirir el lock
durante la descarga.

Pruebas reales:

- watcher focal final: `11 passed`;
- catálogo/parser/downloader afectados: `114 passed`;
- migraciones completas: `35 passed in 235.75s`, incluidos fresh, ambas
  cabezas hermanas al merge y a `0039`, downgrades, inmutabilidad y
  `alembic check`.

Entrada de job cero-argumentos:
`app.reference_layers.catalog_watcher.run_siur_catalog_update_check_job`.
Queda conectar este entrypoint al scheduler diario del orquestador.

### Smoke local prepromoción de GeoServer

- Worktree:
  `/home/dev/proyectos/asistente_ayuntamientos-worktrees/local-operation-smoke-20260726`
- Rama: `codex/local-operation-smoke-20260726-20260726`
- Sesión: `local-operation-smoke-20260726`, estado `paused`
- Base original: `07e46d5`
- Commit:
  `0e25004` (`feat: validate local map operations before promotion`)
- Estado Git: limpio

Implementa un smoke local por estilo antes de promover: mapa, leyenda cuando
exista e `identify` GeoJSON cuando la capa sea consultable. La evidencia queda
en las estadísticas del run y un fallo impide la promoción, conservando la
versión activa. Los tiles baked registran solo su operación de mapa real. El
transporte a GeoServer queda restringido a loopback numérico.

Pruebas reales:

- focales: `73 passed`;
- batería cartográfica/de referencia: `524 passed`;
- `git diff --check`: correcto.

Ambos propietarios han entregado handoff explícito. Sus ramas pueden
inspeccionarse; integrar solo después de revisar los diffs y ejecutar las
pruebas sobre la combinación.

## Bloqueos de producto todavía abiertos

P0 antes de considerar la entrega terminada:

1. Integrar y revisar los dos cierres paralelos.
2. Persistir una estrategia exacta para cada una de las 227 capas hoja:
   `vector`, `raster`, `tiles`, `composition` o `blocked`; las composiciones
   deben declarar dependencias sin ciclos y todo bloqueo debe tener evidencia.
3. Persistir paridad de estilo `exact`, `adapted`, `baked` o `missing`; copiar
   símbolos a almacenamiento local y bloquear la entrega si falta un recurso.
4. Ejecutar migraciones fresh y desde las dos cabezas hermanas, y después
   actualizar la base real desde `20260722_0034`.
5. Ejecutar bootstrap, sincronizar, validar y promover las 227 capas. El estado
   final esperado es 227 estrategias explícitas, 227 capas servibles desde
   artefactos locales o bloqueadas con razón revisable, y cero proxy remoto.
6. Probar offline el flujo completo: catálogo, teselas/mapa, estilos, leyenda,
   identify, promoción, rollback y continuidad después de reinicio.

P1 operativo:

- exponer métricas de bytes, duración, próxima comprobación, validación y último
  error;
- añadir una interfaz de operador para rollback con target, generación
  esperada, actor, motivo y dry-run;
- definir retención/GC conservando al menos activa y anterior;
- añadir herramienta/runbook de recuperación y simulacro de restauración;
- conservar localmente la metadata descriptiva;
- cuantificar geometrías reparadas y documentar contratos por fuente;
- limitar explícitamente la cuota de GeoWebCache y comprobar margen de
  almacenamiento/carga.

## Orden exacto al reanudar

1. Desde el checkout de coordinación:

   ```bash
   cd /home/dev/proyectos/asistente_ayuntamientos
   pwd
   git status --short --branch
   git remote -v
   git branch --show-current
   scripts/codex-session audit
   scripts/codex-session list
   scripts/codex-session runtime status
   ```

2. Confirmar que ambos workers siguen `paused` y limpios. Revisar los commits,
   diffs y pruebas antes de llevarlos a
   `codex/siur-local-mirror-20260722`.
3. Integrar en este orden `7a7b912`, `ff610f3`, `7929abe` y `0e25004`,
   resolviendo cualquier solape de forma explícita. Después conectar la
   invocación diaria del watcher en el scheduler.
4. Ejecutar focales, suite de referencia, suite backend completa y matriz de
   migraciones desde fresh, desde `20260723_0037` y desde
   `20260722_0034`.
5. Implementar matriz de estrategias, paridad de estilos y los P0 restantes en
   ramas/worktrees nuevos.
6. Solo con código y pruebas limpios adquirir el runtime:

   ```bash
   scripts/codex-session runtime acquire \
     --owner root-siur-local-mirror-20260722 \
     --task siur-local-mirror
   ```

7. Reconstruir servicios, comprobar los binarios GDAL, migrar desarrollo,
   ejecutar bootstrap y sincronizar muestras vector, ráster y tiles antes de la
   carga completa.
8. Volver a medir disco. La estimación anterior era aproximadamente 15 GB para
   PNOA, 30 GB para IGN Base y 17 GB para MTN, además de derivados y reserva.
9. Ejecutar la carga completa con capacidad observada, verificar promoción,
   continuidad y rollback, y después la prueba offline integral.
10. Leer las skills de verificación de navegador antes de comprobar el
    frontend en puerto 3000.

No fusionar en `main` hasta cerrar los P0, repetir la verificación integral y
realizar una auditoría requisito por requisito con evidencia.
