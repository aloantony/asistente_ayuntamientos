# Handoff del espejo local SIUR — 2026-07-26

Estado pausado por petición del usuario antes de agotar el límite de uso. La
entrega **no está terminada ni lista para `main`**. Todo el trabajo aceptado y
los tres frentes en curso están guardados en commits; los cuatro worktrees
implicados están limpios y sus sesiones constan como `paused`.

No se ha adquirido el lease del runtime, no se ha migrado la base de desarrollo
y no se ha reconstruido el stack compartido. Ninguna de estas ramas se ha
enviado al remoto.

## Punto de reanudación principal

- Worktree:
  `/home/dev/proyectos/asistente_ayuntamientos-worktrees/siur-local-mirror`
- Rama: `codex/siur-local-mirror-20260722`
- HEAD: `6ed56a4` (`fix: honor SIUR base layer visibility`)
- Upstream de comparación: `origin/main`
- Divergencia al pausar: 39 commits por delante y 0 por detrás
- Sesión/owner: `root-siur-local-mirror-20260722`, estado `paused`
- Estado Git: limpio
- Runtime compartido: sin lease
- Base de desarrollo observada, sin modificar: Alembic `20260722_0034`

El objetivo sigue siendo almacenar y versionar localmente las 227 capas SIUR,
renderizar desde infraestructura propia, comprobar cambios periódicamente,
promover/retirar versiones de forma segura y verificar el flujo completo sin
dependencias cartográficas remotas en el plano de servicio.

## Trabajo ya integrado

Además de almacenamiento content-addressed, adquisición endurecida, ingesta
vectorial/ráster, publicación GeoServer, MBTiles, estilos SLD, cobertura finita
y lifecycle durable, esta rama contiene los últimos cierres:

- `b432a71`: siembra WMS mediante superteselas 8x8 (2048 px) y degradación
  controlada 8 -> 4 -> 2 -> 1 ante rechazo dimensional.
- `59ac86b`: fixture de adquisición alineado con el esquema de fallback.
- `86baf31`: orden temporal determinista en las pruebas de estado del espejo.
- `6ed56a4`: una base SIUR oculta o con opacidad cero deja de renderizarse.

Validación real acumulada sobre la rama de integración:

- backend focal combinado: `102 passed`;
- suite backend completa antes de `86baf31`: `1315 passed, 1 failed`; el único
  fallo era precisamente el reloj no determinista corregido por `86baf31`;
- regresión de ese archivo después de la corrección: `5 passed`;
- frontend Vitest: `11 passed`;
- frontend typecheck sin incremental: correcto;
- frontend lint: 0 errores y 11 avisos preexistentes;
- frontend build de producción: correcto, 23 páginas estáticas.

La suite backend completa todavía debe repetirse sobre la combinación final.

## Checkpoints paralelos preservados

### 1. Fuentes WMS revisadas y capacidad

- Worktree:
  `/home/dev/proyectos/asistente_ayuntamientos-worktrees/siur-reviewed-wms-candidates`
- Rama: `codex/siur-reviewed-wms-candidates-20260726`
- Base: `b432a71`
- Commit: `c9d7705` (`feat: optimize reviewed SIUR tile sources`)
- Sesión: `reviewed-wms-capacity-20260726`, estado `paused`
- Estado Git: limpio

Incluye WMS oficiales preferentes para PNOA, IGN Base y MTN con prioridad 40,
WMTS original como fallback con prioridad 50, equivalencias exactas
versionadas, superteselas limitadas a perfiles SIUR y preflight estratificado
por zoom que conserva cuota, límite de archivo y reserva real de disco.

Pruebas reales:

- focal: `99 passed, 1 deselected`;
- suite de referencia: `368 passed`, salvo el fixture de reloj preexistente en
  su base y ya corregido en integración por `86baf31`;
- `compileall` y `git diff --check`: correctos;
- la suite global se detuvo al 54 % por este checkpoint, sin fallos nuevos.

Es el primer commit candidato a integrar, pero después hay que repetir la suite
combinada sobre la rama principal.

### 2. Continuidad de versiones

- Worktree:
  `/home/dev/proyectos/asistente_ayuntamientos-worktrees/siur-version-continuity`
- Rama: `codex/siur-version-continuity-20260726`
- Base exacta: `86baf31`
- Commit: `2b7b32f` (`fix: preserve SIUR mirror version continuity`)
- Sesión: `version-continuity-20260726`, estado `paused`
- Estado Git: limpio

Conserva servible la versión local activa basada en su run/snapshot histórico
íntegro aunque cambie la definición mutable de la fuente; exige a la vez que la
capa siga activa en el snapshot actual. Añade rollback auditado entre snapshots
y cierre fail-closed ante run, snapshot o asset manipulados. El estado distingue
`serving_previous`.

Validación real:

- `git diff --check`: correcto;
- parseo AST de los cinco archivos Python: correcto;
- `pytest`: **no ejecutado** antes de pausar.

No integrar como terminado hasta ejecutar:

```bash
python -m pytest \
  tests/test_local_reference_delivery.py \
  tests/test_reference_mirror_lifecycle.py -q
```

Hay que prestar atención a SQL/PostgreSQL, especialmente al bloqueo
`with_for_update(of=ReferenceLayer)` y al helper de rollback cross-snapshot.

### 3. Orquestador, reanudación y chequeo de píxeles

- Worktree:
  `/home/dev/proyectos/asistente_ayuntamientos-worktrees/mirror-orchestrator-final`
- Rama: `codex/mirror-orchestrator-final-20260726`
- Base de la rama: `b432a71`
- Commits:
  - `3117535` (`chore: checkpoint reference mirror orchestration`)
  - `67cc9d6` (`fix: harden resumable reference mirror checks`)
- Sesión: `orchestrator-final-20260726`, estado `paused`
- Estado Git: limpio

El primer commit incorpora worker/scheduler, bootstrap, configuración de
capacidad y Compose. El segundo conserva ETag/manifiesto/estadísticas al
reanudar, deja reclaimable un run interrumpido por SIGTERM, evita un segundo
coordinador de fallback, fija 8 GiB como techo de origen geoespacial y añade
muestreo determinista de píxeles remotos contra los MBTiles activos. Las fuentes
tiles tienen comprobación diaria y refresco completo semanal.

Validación final de este checkpoint:

- compilación Python: correcta;
- `git diff --check`: correcto;
- suite completa anterior: llegó a `828 passed` y reveló un fixture de
  adquisición incompatible con lifecycle 0037; su corrección ya está en la
  integración (`59ac86b`), pero no se repitió la suite.

Este checkpoint **no se debe integrar sin revisión y pruebas**. Se solapa con
`mirror_lifecycle.py` del checkpoint de continuidad y con `tile_seed.py` del de
capacidad. Además quedan dos cierres P0 expresamente sin implementar:

1. bloquear por defecto el proxy cartográfico remoto en catálogo y rutas;
2. comparar estructura y contenido razonable con la versión activa antes de
   promover (tipo, CRS, bounds, schema y caída masiva de features).

## Orden seguro al reanudar

1. Repetir la auditoría Git y confirmar que las cuatro sesiones siguen
   `paused` y limpias. Hacer `git fetch origin` y comprobar la base, sin iniciar
   aún el runtime.
2. Integrar o rebasar primero `c9d7705` sobre
   `codex/siur-local-mirror-20260722`; ejecutar sus pruebas focales y la suite de
   referencia.
3. Rebasar/probar `2b7b32f` sobre esa combinación. Ejecutar las dos suites
   focales PostgreSQL y resolver cualquier incompatibilidad antes de
   cherry-pick.
4. Portar `3117535` + `67cc9d6` sobre la combinación ya validada, resolviendo
   explícitamente los solapes. Lifecycle 0037 debe seguir siendo el único
   coordinador de fallback.
5. Añadir las regresiones que faltan al orquestador y cerrar el proxy remoto
   fail-closed y la comparación pre-promoción.
6. Implementar la comprobación diaria durable del catálogo
   `settings.json`: descarga condicional fijada, hash canónico, evidencia
   observable y estado `update_available`; un cambio de catálogo no debe
   aplicarse automáticamente sin revisión.
7. Ejecutar suite backend completa y migraciones fresh y deployed desde
   `20260722_0034` a `head`, incluidos downgrades soportados.
8. Solo entonces adquirir el lease:

   ```bash
   scripts/codex-session runtime acquire \
     --owner root-siur-local-mirror-20260722 \
     --task siur-local-mirror
   ```

   Reconstruir servicios, migrar desarrollo y ejecutar bootstrap real.
9. Sincronizar y promover muestras vector, ráster y tiles; después iniciar la
   carga completa de las 227 capas con capacidad observada. Verificar fallback,
   reanudación, promoción y rollback.
10. Verificar frontend en puerto 3000 y cortar la salida remota del plano de
    servicio para comprobar mapa, teselas, estilos, leyendas e identify solo
    desde copias locales.

## Riesgos y trabajo todavía no realizado

- La base de desarrollo seguía mostrando 227 capas con `delivery_mode=proxy`;
  aún no existen 227 versiones locales promovidas.
- El backend todavía puede usar el proxy remoto si no se integra el cierre P0.
- No existe todavía comparación estructural activa antes de promoción.
- Falta el watcher durable diario del catálogo SIUR, distinto del chequeo de
  píxeles de fuentes tiles.
- No se ha ejecutado bootstrap/carga real, ni prueba offline integral, ni
  verificación visual en puerto 3000 con este código.
- Falta completar validación ráster (nodata, overviews, resolución), smoke de
  leyenda/identify, métricas operativas, interfaz explícita de rollback y
  política de retención/recuperación compatible con la inmutabilidad.
- Las estimaciones reales previas fueron aproximadamente 15 GB PNOA, 30 GB IGN
  Base y 17 GB MTN; antes de una carga completa hay que volver a medir espacio y
  respetar cuota y reserva.

No fusionar en `main` hasta resolver estos puntos y repetir la verificación
integral.
