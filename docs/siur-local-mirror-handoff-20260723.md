# Handoff del espejo local SIUR — 2026-07-23

Estado pausado por petición del usuario antes de agotar el límite de uso. La
entrega no está terminada ni lista para `main`, pero todo el trabajo aceptado
está en commits y los worktrees implicados están limpios.

## Rama de integración

- Worktree: `/home/dev/proyectos/asistente_ayuntamientos-worktrees/siur-local-mirror`
- Rama: `codex/siur-local-mirror-20260722`
- HEAD antes de este documento: `a689e31`
- Sesión: `root-siur-local-mirror-20260722`
- El runtime compartido fue liberado antes de pausar.

`a689e31` contiene almacenamiento content-addressed, adquisición endurecida,
ingesta vectorial/ráster, publicación GeoServer, archivos MBTiles, entrega y
estado local en frontend, estilos SLD inmutables, cobertura SIUR finita y el
ciclo durable de fuente primaria, reintentos, fallback, promoción y rollback.
También contiene `66e1fb2`, que corrige dos incompatibilidades observadas en
servicios reales: estilo WMTS/WMS vacío y un enlace UTM defectuoso de IGN que no
debe invalidar su enlace WebMercator correcto.

Pruebas realizadas sobre esas dos correcciones:

```text
python -m pytest tests/test_reference_source_probes.py tests/test_reference_acquisition.py -q
59 passed
```

El commit de lifecycle integrado (`a689e31`) pasó en su rama de origen 1.225
tests backend, 283 tests de referencia/SIUR y las rutas Alembic fresh y
`0036 -> 0037 -> 0036`. Debe repetirse la suite sobre la combinación final.

## Checkpoint terminado que sí se puede integrar

- Rama: `codex/siur-wms-supertile-20260722`
- Commit: `99b2447` (`feat: seed reviewed WMS sources with supertiles`)
- Estado: limpio, sesión pausada, no publicado.
- Validación: 86 tests focales y 1.308 tests backend; 2 warnings preexistentes.

Integra peticiones WMS 8x8 (máximo 2048x2048), recorte local a teselas de 256,
bordes parciales, conservación de alfa PNG/JPEG y reducción controlada
8 -> 4 -> 2 -> 1 solo ante rechazos dimensionales. El contrato de fuente es
`wms_supertile_size=8` y exige uno de los perfiles SIUR revisados.

Primer paso recomendado al reanudar:

```bash
git cherry-pick 99b2447
```

Después hay que añadir candidatos WMS oficiales, con prioridad anterior al
WMTS original y conservando WMTS como fallback, para:

- PNOA: `https://www.ign.es/wms-inspire/pnoa-ma`,
  `OI.OrthoimageCoverage`, JPEG.
- IGN Base: `https://www.ign.es/wms-inspire/ign-base`,
  `IGNBaseTodo-nofondo`, PNG transparente.
- MTN: `https://www.ign.es/wms-inspire/mapa-raster`,
  `mtn_rasterizado`, JPEG (equivale al `MTN` del WMTS del catálogo).

Los tres endpoints oficiales respondieron correctamente a un GetMap real de
2048x2048: 679.815 B, 4.221.191 B y 1.002.800 B respectivamente.

## Checkpoint que todavía no se debe integrar

- Rama: `codex/mirror-orchestrator-20260722`
- Commit: `7895c21` (`chore: checkpoint reference mirror orchestration`)
- Estado: limpio, sesión pausada, no publicado.

El checkpoint incluye scheduler/worker, bootstrap automático, configuración de
capacidad y Compose. Pasaron 163 tests focales antes de las últimas correcciones;
después pasan `py_compile` y `git diff --check`. La suite completa se interrumpió
al 45 % y había aparecido un fallo sin traza capturada. No hacer cherry-pick
hasta:

1. añadir regresiones para los límites vector (2 GiB), ráster (8 GiB), driver,
   SHA y la carrera de fallback terminal;
2. identificar el fallo de la suite completa;
3. garantizar que SIGTERM no consume un fallback;
4. conservar manifiesto, ETag y estadísticas al reanudar;
5. resolver el chequeo diario de contenido de teselas: capabilities estable no
   demuestra que los píxeles no hayan cambiado;
6. rebasarlo sobre `a689e31` más `99b2447`, usando lifecycle 0037 como único
   coordinador durable de fallback.

Los defaults ya acordados son 256 GiB por blob/archive, cuota configurable de
1 TiB y reserva de 20 GiB. El host de desarrollo tenía 166 GiB libres al
pausar. Las preflight reales de 64 muestras por fuente proyectaron, de forma
deliberadamente conservadora:

| Fuente | Teselas | Proyección | Estimación por promedio muestral |
| --- | ---: | ---: | ---: |
| PNOA JPEG | 1.308.502 | 50,8 GB | ~15 GB |
| IGN Base PNG | 1.308.502 | 197,0 GB | ~30 GB |
| MTN JPEG | 1.308.502 | 57,9 GB | ~17 GB |

Al reanudar hay que revisar el estimador conservador de IGN Base sin eliminar
el fallo temprano por falta real de espacio. El inventario actual suma 227
capas, 605 candidatos y 32 capas que requieren archivo de teselas; 20 son
ortofotos históricas z0-z15 y el resto usa el perfil SIUR z0-z16.

## Orden de cierre pendiente

1. Integrar `99b2447`, resolver candidatos WMS equivalentes y capacidad.
2. Terminar y validar el orquestador; integrarlo solo tras suite limpia.
3. Ejecutar migraciones fresh y deployed `0036 -> 0037 -> head` y downgrade.
4. Reconstruir backend, worker, scheduler y GeoServer con el lease de runtime.
5. Ejecutar bootstrap real: 227 capas, una primaria por capa y 605 candidatos.
6. Sincronizar muestras vector, ráster y tiles; luego iniciar la carga completa
   con capacidad comprobada, observando promoción, fallback y rollback.
7. Ejecutar suites backend/frontend y verificar el visor en puerto 3000.
8. Cortar salida externa del plano de servicio y confirmar mapa, teselas,
   estilos, leyendas e identify exclusivamente locales.
9. Actualizar documentación operativa, dejar la rama limpia, subirla y preparar
   la integración en `main` solo cuando todo lo anterior pase.
