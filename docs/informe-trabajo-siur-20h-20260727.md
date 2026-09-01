# Informe verificable del trabajo SIUR realizado

Fecha del informe: 27 de julio de 2026

Rama de trabajo: `codex/siur-local-mirror-20260722`

Estado de integración: **no está integrado en `main`**

## Resumen honesto

Durante la ventana examinada se construyó e integró en una rama de trabajo una
parte extensa de la infraestructura para conservar, versionar, validar y servir
cartografía SIUR desde el propio servidor. También se consiguió publicar y
renderizar localmente una primera capa real: **Cuadrícula Eurostat 100x100**.

Sin embargo, el objetivo completo no está terminado:

- solo una capa está activa de extremo a extremo en el runtime;
- la mayoría de las estrategias del catálogo están preparadas, pero sus datos
  todavía no se han descargado, validado y promocionado;
- el programador diario no se ha dejado operativo y verificado;
- hay cambios finales sin commit y controles de seguridad con defectos
  detectados que todavía deben corregirse;
- la rama está 149 commits por delante de `origin/main`, pero no se ha
  fusionado;
- el paquete final de revisión existe en otra rama, pero aún no se ha integrado
  en esta;
- no hay una ejecución completa de tests posterior a los últimos cambios sin
  commit.

Por tanto, no sería correcto decir que no se ha hecho nada, pero tampoco sería
correcto presentar el trabajo como terminado o listo para `main`.

## Resultado visible que sí existe

El 27 de julio se completó una publicación real con estos datos persistidos:

| Dato | Valor |
| --- | --- |
| Capa | `287` — Cuadrícula Eurostat 100x100 |
| Servicio | `5` |
| Fuente autorizada | `1842` |
| Revisión de espejo | `1` |
| Estado de autorización | `authorized` |
| Tipo de entrega | Vector local |
| Versión activa | `1` |
| Generación activa | `1` |
| Estado del run | `succeeded` |
| Intento final | `3` |
| Número de entidades | `19` |
| Hash de contenido | `43c45d2741e08070f58c67dbe14bc4f2de5591b63bc85f0ce5770a3cea449b91` |
| Hash de manifiesto | `bea4087acc4d111ff02c9e6e7a8d847382bfd1ea0e256ac41e6601355598ef87` |
| Tabla local | `reference_data.m_l7z_r1_v_528ebe088e798fd7fd4c83cd` |
| Política de conservación | Todo el historial publicado queda protegido |

La descarga oficial de Eurostat se utilizó como entrada transitoria. El
producto persistido contiene las 19 celdas de 100 km que intersectan Castilla y
León y únicamente los campos revisados `GRD_ID`, `X_LLC` y `Y_LLC`; no incluye
datos de población.

La publicación local se comprobó contra GeoServer con tres estilos:

| Estilo | Color comprobado | Píxeles visibles | Tamaño de tesela |
| --- | --- | ---: | ---: |
| Morado | `#6d28d9` | 1.347 | 6.677 bytes |
| Blanco | `#ffffff` | 1.347 | 5.465 bytes |
| Fucsia | `#e6007e` | 1.347 | 6.604 bytes |

La tesela morada comprobada fue `z=8, x=124, y=95`. Se obtuvo directamente del
GeoServer local, no de un WMS remoto.

El runtime actual también responde:

- `http://127.0.0.1:3000/api/health`: HTTP 200;
- `http://127.0.0.1:3000/mapa`: HTTP 200;
- backend, frontend, PostgreSQL, Redis y GeoServer están levantados;
- GeoServer aparece como `healthy`.

La ruta de teselas que usa el frontend requiere un usuario autenticado con
permiso `map.view`. Por eso la comprobación sin credenciales se hizo directamente
contra el WMS local. No se fabricó ni reutilizó una sesión de usuario.

## Qué se construyó

### Persistencia y ciclo de vida

- Modelo persistente de fuentes, ejecuciones, artefactos, versiones, activos y
  promociones.
- Versiones inmutables ligadas a hashes de contenido, manifiesto, catálogo y
  revisión.
- Promoción atómica de una versión validada.
- Historial de promociones y generación activa para evitar servir una versión
  obsoleta.
- Rollback con comprobación de que los activos físicos siguen existiendo.
- Retención de versiones, artefactos de adquisición y evidencia de promoción.
- Recuperación y herramientas operativas para inspección, reactivación y
  rollback.

### Descarga y generación local

- Descarga segura y limitada, fuera de transacciones largas de base de datos.
- Almacén de blobs direccionado por contenido.
- Ingesta validada de GeoPackage a PostGIS.
- Capturas WFS convergentes y paginadas.
- Preparación de archivos raster y pirámides de teselas.
- Precálculo de capacidad para evitar comenzar espejos que no caben.
- Derivación local de la cuadrícula Eurostat usando una máscara revisada.
- Soporte de fuentes compuestas y archivos ZIP con evidencia exacta.

### Renderizado y estilos

- Publicación de vectores y ráster en GeoServer local.
- Integración con GeoWebCache y cuotas de almacenamiento.
- Estilos SLD locales versionados y vinculados a la evidencia revisada.
- Comprobación de paridad de estilos antes de promover una capa.
- Validación de mapas, leyendas e identificación antes de activar una versión.
- Soporte de tres estilos locales para la cuadrícula Eurostat publicada.
- Fallback a WMS para capas IDECyL cuya paridad local no puede demostrarse.

### Actualizaciones y seguridad

- Fuentes con intervalo de comprobación de 86.400 segundos.
- Detección de cambios de catálogo y de estilos oficiales.
- Bloqueo por drift de catálogo, fuente, autorización o generación.
- Autorización explícita por fuente para descargar, conservar y servir
  localmente.
- Reconciliación versionada de estrategias para 227 asignaciones:
  192 vectoriales, 2 ráster, 31 de teselas y 2 bloqueadas.
- Desactivación del proxy remoto por defecto en el modo offline.
- Fences de versión en las URLs de frontend para que una tesela antigua no se
  mezcle con una versión nueva.
- Metadatos locales canónicos y verificables por hash.

### Runtime y frontend

- Topología Docker para PostgreSQL/PostGIS, Redis, backend, worker, scheduler,
  GeoServer/GeoWebCache y frontend.
- Montaje de almacenamiento de referencia en modo solo lectura para los
  consumidores.
- Reconstrucción y reinicio autorizado de backend y frontend.
- Catálogo y URLs de teselas, leyendas, metadatos e identificación consumibles
  por el mapa.
- Pruebas de recuperación del runtime frontend.

## Evidencia de Git

La rama actual contiene:

- 149 commits por delante de `origin/main`;
- 114 commits alcanzables desde la rama con fecha posterior al 26 de julio a
  las 16:30, incluidos los trabajos de subagentes integrados;
- 187 archivos diferentes respecto a `origin/main`;
- 174.482 inserciones y 1.691 eliminaciones. Una parte importante corresponde a
  evidencia, catálogos, migraciones y tests, no únicamente a lógica ejecutable.

Algunos commits representativos:

| Commit | Contenido |
| --- | --- |
| `5415936` | Endurecimiento de ejecuciones reanudables |
| `e3912c2` | Vigilancia segura de actualizaciones de catálogo |
| `f44efe9` | Validación de operaciones locales antes de promoción |
| `86dd75c` | Programación persistente de comprobaciones |
| `09aed49` | Estrategias de espejo persistidas |
| `24f49c4` | Operaciones administrativas seguras |
| `456ce35` | Paridad obligatoria de estilos locales |
| `b196546` | Espejo de conjuntos SIUR revisados |
| `7a2375a` | Autorización explícita de espejo |
| `f7c9393` | Estilos SIUR locales revisados |
| `d6398b5` | Metadatos cartográficos locales canónicos |
| `cf4a358` | Topología de aceptación SIUR offline |
| `9e26888` | Cobertura del ciclo actualización/rollback |
| `30b6cd6` | Preflight agregado de capacidad de teselas |
| `5efdcfd` | Ingesta de vectores GeoPackage |
| `afb4231` | Derivación local de cuadrículas Eurostat |
| `3bcf344` | Estilos locales de la cuadrícula |
| `ff65fdb` | Smoke tests representativos de GeoServer |
| `841f5c3` | Contrato Docker offline endurecido |
| `e15c6c0` | Reconciliación segura de estrategias |

Existe además el commit `12ece36` en
`codex/siur-review-packet-20260727`, con el paquete legible de revisión para
`source=1842`, `layer=287`, `service=5`. Todavía no se ha aplicado a la rama
principal de este trabajo.

## Pruebas ejecutadas

Resultados registrados antes de los últimos cambios sin commit:

- suite backend completa: **2.135 tests superados**;
- suite integrada relevante: **198 tests superados**;
- frontend: **27 tests superados**, type-check y build completados;
- scripts operativos: **14 tests superados**;
- administración GeoServer: **111 tests superados** durante la corrección;
- paquete de revisión separado: **41 tests superados**;
- readiness: 5 tests focalizados y 45 integrados superados antes de la revisión
  crítica posterior.

Además de los tests, se verificó en vivo:

- persistencia de la autorización exacta;
- reconciliación del catálogo;
- descarga y derivación de la fuente Eurostat;
- ingestión en PostGIS;
- publicación en GeoServer;
- tres mapas y tres leyendas;
- promoción de la versión;
- estado activo después de reiniciar backend y frontend;
- respuesta HTTP 200 del frontend y del health check.

Estas cifras **no equivalen a una suite final verde del estado actual**. Después
se modificaron seis archivos y se añadieron dos archivos nuevos; todavía falta
repetir las pruebas amplias.

## Problemas reales encontrados

### 1. El catálogo no contenía mapas locales listos

La importación previa había creado el catálogo y referencias a servicios, pero
no había descargado y promocionado automáticamente los datos. Por eso el
frontend conocía las capas, pero mostraba que no tenían renderizador.

### 2. No todas las fuentes se pueden tratar igual

Las capas llegan por WMS, WFS, WCS, WMTS, XYZ, ArcGIS y descargas de archivos.
Cada tipo necesita una estrategia distinta de captura, validación, conservación
y renderizado. El trabajo se expandió desde “mostrar capas” hasta construir el
sistema completo para todos esos casos.

### 3. La autorización general del catálogo seguía en `pending`

Se implementó una autorización específica por fuente para evitar que una
descarga local eludiera el control que antes solo protegía el proxy WMS. La
fuente 1842 sí está autorizada y publicada. El estado general de licencia del
servicio sigue apareciendo como `pending`, aunque ya no bloquea esa fuente
revisada; esta doble representación es confusa y debe aclararse.

### 4. GeoServer real no se comportó como los mocks

Durante la primera publicación aparecieron tres incompatibilidades que CI no
detectaba:

- el alta de un estilo de capa devuelve 406 si se envía
  `Accept: application/json`; GeoServer exige una aceptación más amplia;
- la respuesta real de la capa puede omitir `enabled`;
- la URL de un estilo asociado a una capa tiene una forma diferente a la URL de
  un estilo del workspace.

Se corrigieron las tres. Los fallos originales quedaron registrados y el mismo
artefacto inmutable se publicó finalmente en el tercer intento, sin repetir la
descarga.

### 5. El frontend exige autenticación

El mapa en el puerto 3000 abre correctamente, pero la capa se solicita mediante
una API autenticada. Sin credenciales de un usuario real no se completó el
recorrido visual dentro de la sesión del navegador. Sí se verificó el mismo
render local directamente en GeoServer.

## Qué sigue sin terminar

### Bloqueos técnicos inmediatos

1. El reintento administrativo de publicación permite ahora casos demasiado
   amplios. Debe limitarse a fallos reintentables, bloquear cadenas fallback y
   conservar los timestamps anteriores en la auditoría.
2. El informe de readiness tiene tres falsos positivos potenciales:
   - no ata la versión activa a la fuente y estrategia reconciliadas;
   - un run huérfano `queued` o `running` puede ocultar indefinidamente una
     actualización vencida;
   - evidencia estructuralmente corrupta devuelve código 1 en vez de código 2.
3. Falta integrar el paquete legible de revisión `12ece36`.
4. Falta ejecutar de nuevo las pruebas focalizadas y la suite amplia.
5. Falta revisar el diff, crear el commit final, subir la rama y fusionarla.

### Bloqueos funcionales del objetivo completo

1. Solo la fuente 1842 tiene la revisión exacta aplicada. La confirmación
   persistida no autorizó automáticamente las otras 226 asignaciones.
2. Falta ejecutar la adquisición, validación y promoción de las demás fuentes
   técnicamente replicables.
3. Falta dejar el scheduler diario activo y comprobar un ciclo real sin cambios
   y otro con actualización.
4. Falta verificar la experiencia completa del mapa con un usuario autenticado.
5. La máscara IGN usada en la derivación quedó retenida como artefacto de
   entrada. No es el GeoPackage bruto de Eurostat, pero debe decidirse si la
   política “conservar solo el derivado” también exige no retener esa máscara.
6. El tamaño del activo principal PostGIS figura como incompleto porque
   `size_bytes` es nulo; no impide renderizar, pero debe resolverse en la puerta
   operativa.

## Cambios actuales sin commit

El árbol de trabajo no está limpio:

```text
 M backend/app/reference_layers/geoserver_admin.py
 M backend/app/reference_layers/mirror_admin.py
 M backend/app/reference_layers/mirror_lifecycle.py
 M backend/app/reference_layers/mirror_orchestrator.py
 M backend/tests/test_geoserver_admin.py
 M docs/siur-mirror-operations.md
?? backend/app/reference_layers/mirror_readiness.py
?? backend/tests/test_reference_mirror_readiness.py
?? docs/informe-trabajo-siur-20h-20260727.md
```

Los seis archivos seguidos por Git suman actualmente 705 inserciones y 50
eliminaciones. Los dos archivos funcionales nuevos de readiness suman 1.169
líneas; el tercer archivo nuevo es este informe.

## Por qué se alargó tanto

Hubo complejidad técnica real, sobre todo al pasar de referencias remotas a
datos locales versionados y al probar contra un GeoServer real. Pero eso no
justifica por sí solo la mala entrega.

El problema de ejecución fue que se intentó abarcar la arquitectura completa,
las 227 estrategias, todos los tipos de fuente, seguridad, recuperación,
estilos, capacidad y operación antes de cerrar y mostrar una entrega vertical
pequeña. También se abrieron demasiados frentes y subagentes en paralelo. Eso
produjo muchos commits y tests, pero retrasó el resultado que más importaba:
una capa visible y una rama limpia lista para integrar.

Debí haber entregado primero la capa Eurostat operativa, con una explicación
clara de que era la única autorizada, y solo después ampliar el sistema. El
estado actual refleja trabajo sustancial, pero una gestión de alcance
deficiente y una comunicación insuficiente.

## Comandos para reproducir la auditoría

Desde el worktree:

```bash
git status --short --branch
git rev-list --count origin/main..HEAD
git diff --shortstat origin/main...HEAD
git log --oneline origin/main..HEAD
git diff --stat HEAD
```

Estado persistido de la capa:

```bash
docker compose exec -T backend \
  python -m app.reference_layers.mirror_admin \
  status --provider-key siur --layer-id 287
```

Estado del runtime:

```bash
docker compose ps
curl --fail http://127.0.0.1:3000/api/health
curl --fail http://127.0.0.1:3000/mapa
```

## Conclusión

Hay una base técnica amplia y una primera capa real servida localmente, pero el
objetivo prometido —todas las capas replicables, scheduler operativo,
verificación integral y rama lista para integrar— **no está cumplido**. La
prioridad correcta ahora no es añadir más alcance, sino cerrar los tres defectos
detectados, repetir pruebas, integrar el paquete de revisión, dejar el árbol
limpio y producir una entrega verificable antes de continuar con nuevas capas.
