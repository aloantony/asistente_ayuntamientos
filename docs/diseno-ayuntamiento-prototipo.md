# Diseño: pantalla Ayuntamiento e Instalaciones (prototipo de Claude Design)

Actualizado: 2026-07-30. Cuerpo del Ayuntamiento terminado; la Fase C depende de SIUR (§3 bis).

Fuente: proyecto de Claude Design `bb6236ef-e522-46db-a8ce-2f9097b2a3d1`, fichero
`uploads/ZIP del  HTML de claude Design/Pantalla Principal.dc.html` (1.437 líneas, tres pantallas:
Inicio, Ayuntamiento e Instalaciones). Copia local del HTML:
`~/.claude/plans/assets/pantalla-principal.dc.html`.

Este documento no describe el estado implementado —para eso están `arquitectura.md` y `requisitos.md`—
sino **lo que falta para que la aplicación se parezca al prototipo**, en qué orden y con qué decisiones
previas. Es el equivalente para esta pantalla de lo que `diseno-dialogo-voz.md` fue para la voz.

## 1. Punto de partida

Lo ya construido y reutilizable:

- **`municipal_blocks` / `municipal_profiles`** (ADR-034): árbol genérico de bloques por organización
  con padre, orden, `title`, `body` y `data_json`, más el perfil del municipio (nombre mostrado,
  escudo, temperatura). El check de `block_type` **ya admite `epigraph`, `section` e `item`**, así que
  los epígrafes de contenido **no necesitan migración nueva**.
- **`MunicipalWorkspace`** (`/ayuntamiento`): hub de lectura con masthead, selector de organización y
  pestañas fijas Resumen / Normativa / Instalaciones / Personal / Hoja de ruta. Agrega los módulos
  operativos existentes.
- **`assets` + `maintenance`**: inventario municipal (categorías, tipos, activos con `location_id`,
  estado de conservación, materiales, fechas de instalación e inspección) y órdenes de mantenimiento
  con historial append-only. Es, en la práctica, **«fichas y mantenimiento» del prototipo ya hecho**.
- **`ordinances`**: normativa con importación supervisada, revisión, chunks y búsqueda semántica.
- **`documents`** + `LocalStorageService`: subida en streaming con lista blanca, tope, sha256 y claves
  generadas en servidor. El escudo ya lo reutiliza bajo el prefijo `organizations/<id>/brand/`.
- **`geo`**: en `main`, **solo puntos**. `GeoLocation` (punto, lat/lon, municipio, organización) y
  `EntityLocation` (`requirement|project`). `MunicipalMap` es un envoltorio de Leaflet con teselas de
  OpenStreetMap.
- **`reference_layers` (SIUR)**: **la cartografía ya está construida en otra rama**, sin fusionar. Ver
  §3 bis: es lo más importante de este documento antes de tocar la Fase C.

## 2. Inventario: prototipo frente a implementado

### Barra del municipio
| Elemento del prototipo | Estado |
|---|---|
| Escudo 46×56, reemplazable arrastrando una imagen | Hecho |
| Nombre del municipio en Newsreader 24px | Hecho |
| Bloque de temperatura del día | Hecho (Open-Meteo, ADR-034) |
| Botón ⋮ y editor de menú (renombrar, reordenar, borrar) | Hecho |
| Píldora central con desplegables al hover | Hecho (Fase A, `279e2a5`) |

### Pantalla Ayuntamiento (cuerpo)
Siete epígrafes, todos con menú contextual propio (renombrar, subir, bajar, eliminar) y reordenables
por arrastre. **Ninguno está implementado.**

| Epígrafe | Contenido del prototipo |
|---|---|
| Información general | Apartados y elementos anidados, texto editable en línea |
| Estructura de gobierno | Niveles de gobierno con campos libres, historial de gobiernos, miembros (cargo, nombre, partido + campos añadidos) |
| Datos del municipio | Pestañas General (pares clave-valor), Demografía (**gráfica** padrón + hogares, doble eje, editable por años), Clima (**gráfica** mensual máx/media/mín + precipitación, rejilla editable de 12 meses, y evolución interanual), Agua (**carrusel de PDF** con subida, laboratorio, parámetros por año), Patrimonio (fichas con foto) |
| Corporación municipal | Información municipal, alcaldía, tenientes, concejales |
| Teléfonos | Pestañas Servicios / Equipo / Personal, listas editables |
| Archivo | Pestañas Archivo / Fototeca / Crónicas / Himno, con adjuntos (PDF, imágenes, audio), visor de fotos y editor de crónicas |
| Normativa | Categorías, documentos con estado, enlace externo, adjuntos y versiones anteriores archivables |

### Pantalla Instalaciones
| Elemento | Estado |
|---|---|
| Panel cartográfico de 5 pestañas (Base, Territorio, Urbanismo, Infraestructuras, Análisis) | Falta |
| Mapas base: ninguno, relieve, ortofoto, topográfico, catastro | Falta |
| Capas con casilla, muestra de color, «sin datos» y contador | Falta |
| Mapa con búsqueda de capas y elementos, leyenda flotante, pantalla completa | Falta (Leaflet ya existe) |
| Selección de área y exportación del recorte a PNG / JPG / PDF | Falta |
| Editor de capa (color, pictograma) | Falta |
| **Fichas y mantenimiento** (búsqueda, paneles por capa, fotos, resumen, filtros de vencimiento) | **Parcialmente hecho**: `assets` + `maintenance` cubren el dominio; falta la vista del prototipo sobre ellos |
| Herramientas de análisis (medición, dibujo, perfil, comparador) | El propio prototipo las marca «pronto» → **fuera de alcance** |

## 3. Decisiones abiertas (cerrar antes de teclear)

1. **¿Qué pasa con `MunicipalWorkspace`?** Sus cinco pestañas fijas y los siete epígrafes del
   prototipo compiten por la misma pantalla. Tres salidas: (a) el workspace se convierte en el
   epígrafe «Resumen» y los demás se añaden; (b) desaparece y sus datos se reparten entre epígrafes;
   (c) convive en una ruta aparte. **Recomendación: (a)** — conserva el trabajo hecho y encaja con la
   idea de epígrafes reordenables.
2. **Edición en línea.** El prototipo usa `contenteditable` (`gov-edit`). Recomendación: **no**
   replicarlo; usar `input`/`textarea` con guardado al perder el foco, como el editor de menú ya hace.
   Es más accesible, más fácil de validar y coherente con el resto del proyecto.
3. **Adjuntos de los epígrafes** — **CERRADA** (2026-07-30, Anthony): cajón propio, como el escudo.
   Si con el uso se ve que las fotos y crónicas deberían estar en el archivador general, se migra
   entonces con criterio.
4. **Gráficas** — **CERRADA** (2026-07-30, Anthony): SVG propio, sin dependencia. Gana en las dos
   cosas que se pedían: menos problemas (ni ADR, ni cadena de dependencias que auditar en un servicio
   público) y mejor aspecto, porque usa los tokens del proyecto en vez de traer su propia estética.
   **Paleta validada con `scripts/validate_palette.js` de la guía de visualización**, para las dos
   superficies: claro `#eb6834 / #3caf8c / #2a78d6` sobre `#fbf9f3`; oscuro `#d95926 / #199e70 /
   #3987e5` sobre `#2a2928`. En claro el turquesa queda bajo 3:1, lo que obliga a etiqueta visible:
   por eso hay leyenda y vista de tabla siempre.
5 a 8. **Modelo de capas, mapas base externos, el `geoserver` y los GeoJSON** — **RETIRADAS**: las
   responde SIUR, no este documento. Ver §3 bis.
9. **Numeración de ADR.** El último es el 033. Los ADR de estas fases arrancan en el **034**.

## 3 bis. La cartografía ya existe: SIUR (hallazgo del 2026-07-30)

Antes de construir nada de la Fase C hay que saber esto. En la rama
`codex/add-siur-strategy-reconcile-cli-20260727` (y hermanas, **sin fusionar en `main`**) vive un
módulo `backend/app/reference_layers/` de **más de 70 ficheros** con **22 migraciones que `main` no
tiene**, y diez documentos de diseño (`docs/integracion-siur.md`, `siur-local-mirror.md`,
`siur-disaster-recovery.md`, `siur-mirror-authorization-review.md`…).

Su objetivo declarado: que el mapa sea *«un superconjunto verificable del visor SIUR»* — catálogo,
jerarquía, bases, capas, orden, visibilidad, estilos, leyendas, metadatos y operaciones públicas.

Lo que ya resuelve:

- **GeoServer propio** (`docker.osgeo.org/geoserver:3.0.0`, en `127.0.0.1:8081`) con volúmenes de
  datos, caché de teselas y artefactos de referencia. El contenedor «huérfano» de la decisión 7 **es
  ese**: no es un resto olvidado.
- **Modelo de capas**: `reference_services`, `reference_layers`, `reference_layer_styles`,
  instantáneas de catálogo, versiones observadas y comprobaciones de actualización.
- **Espejo local** de cartografía oficial con autorización, cobertura, reconciliación, recuperación
  ante desastres y auditoría de fuentes; evidencias de IDECyL, SIGPAC y PNOA histórico.
- **Proxy y caché WMS**, siembra de teselas y control de cuota — es decir, los mapas base de la
  decisión 6, servidos desde casa en vez de exponer al navegador contra terceros.

**Consecuencia para este plan**: la Fase C **no construye cartografía, la consume**, igual que C4
consume `assets` y `maintenance`. Construir aquí un segundo modelo de capas repetiría —a mucho mayor
coste— el error de duplicar `/ayuntamiento` que ya se cometió al principio de este trabajo.

**Requisito previo**: que SIUR esté fusionado, o al menos su modelo estable. Mientras `main` vaya por
detrás de las ramas de cartografía, seguridad, despliegue y Ayuntamiento a la vez, cualquier fase
nueva se construye sobre una base que no es la verdad.

## 4. Fases

Cada fase termina en uno o varios commits que pasan la validación de README §9. Ninguna deja la
aplicación en un estado peor que el anterior.

### Fase A — Barra del prototipo (pequeña) — HECHA (`279e2a5`)
Sustituir el masthead de `MunicipalWorkspace` por la cabecera del diseño: escudo y nombre a la
izquierda, **píldora central con los apartados y sus desplegables al hover**, temperatura y ⋮ a la
derecha. El selector de organización se conserva, reubicado. No toca el backend: bloques, perfil,
escudo, temperatura y editor ya existen.
*Cerró la decisión 1* en su variante (a): la píldora es la única navegación y las áreas fijas de
`MunicipalWorkspace` conviven en ella con los apartados creados por el usuario. De paso, los elementos
de cada apartado se muestran por primera vez: hasta ahora se guardaban y editaban pero no se pintaban.

### Fase B — Epígrafes de contenido (grande, por sub-fases)
Reutiliza `municipal_blocks` con `epigraph`/`section`/`item` (sin migración) más `body` y `data_json`.
Orden propuesto, de menor a mayor riesgo:

- **B1 Información general** — HECHA (`a9877c6`). Jerarquía epígrafe → apartado → elemento sobre el
  árbol de bloques, sin migración. *Cerró la decisión 2*: campos normales con guardado al perder el
  foco, nada de `contenteditable`.
- **B2 Teléfonos** — HECHA (`0d1958d`). Los apartados ganan un **formato** guardado en `data_json`
  (`text` | `contacts`), también sin migración. El elemento no cambia de forma: `title` es la
  etiqueta y `body` el valor, que en `contacts` se pinta como número marcable.
- **B3 Corporación y Estructura de gobierno** — HECHA (`9014721`). Formato `people`: el nombre en el
  título y una lista ordenada de campos libres por persona en su `data_json`, con tope de 20. Los
  «cargo, nombre, partido» del prototipo son simplemente los campos que se crean de inicio.
- **B4 Archivo** — HECHA (`db3c71a`). *Cerró la decisión 3* por la primera opción: cajón propio bajo
  `organizations/<id>/archive/`, con lista blanca (PDF, imágenes, MP3/OGG), tope propio, límite de
  subida y descarga siempre como adjunto. Los ficheros **no** aparecen en el listado general de
  documentos: es la contrapartida aceptada. Visor de fotos y editor de crónicas siguen pendientes.
- **B5 Datos del municipio** — HECHA. `c06666d` trajo el formato `data` (rejilla de pares dato/valor)
  y la vista previa de imágenes, que es lo que necesitaban General, Patrimonio y la Fototeca.
  `e85ec87` añade el formato `series`: cada elemento es una serie con su unidad y sus puntos, y
  **la unidad decide la gráfica**, de modo que el doble eje del prototipo es imposible por
  construcción y no por disciplina. Con esto quedan cubiertas Demografía, Clima y la evolución de
  parámetros del agua; los PDF de los análisis ya los cubría el formato `files` de B4.
- **B6 Normativa** — HECHA (`6ab66b6`), y mucho menor de lo previsto: la pestaña del workspace ya
  mostraba tipo, título, estado, resumen, materia, fecha, boletín y enlace a la fuente oficial. Solo
  faltaba la agrupación por categoría, que ahora usa `topic`. **No** se duplicó la búsqueda: la real,
  con filtros y coincidencia semántica, ya vive en `/ordenanzas`. Quedan sin construir los adjuntos
  por documento y las versiones anteriores archivables: no existen en el modelo de `Ordinance` e
  inventarlos aquí bifurcaría el dominio.

### Fase C — Instalaciones (consumo de SIUR, no construcción)
- **C1 Modelo de capas** — **NO SE HACE AQUÍ**: lo aporta `reference_layers` (§3 bis). Esta fase se
  limita a leer su catálogo.
- **C2 Importación de los GeoJSON de Fuentelcésped** — **revisar contra SIUR antes**: su espejo local
  y su auditoría de fuentes probablemente ya cubren buena parte, y las que no, deberían entrar por su
  vía y no por una importación paralela.
- **C3 Panel de 5 pestañas, leyenda y búsqueda** — sobre `MunicipalMap`, alimentado por el catálogo de
  `reference_layers`. Es el grueso de lo que queda por hacer.
- **C4 Fichas y mantenimiento** — HECHA (`305e43c`) en su parte independiente: filtro de vencimiento
  (todas / ≤30 días / vencidas) resuelto **en el servidor** con `scheduled_to`, y aviso en rojo de las
  órdenes pasadas de fecha. Consume `assets` y `maintenance`, sin almacén propio. Queda pendiente lo
  que sí depende del mapa: los paneles por capa y la búsqueda de elementos sobre la cartografía.
- **C5 Mapas base y ortofotos** — **ya resuelto por SIUR**: su proxy y caché WMS sirven las bases
  desde casa, así que no hay egreso nuevo del navegador que decidir.
- **C6 Exportación del recorte** a PNG/JPG/PDF.
- Herramientas de análisis: fuera de alcance.

## 5. Riesgos

- **El prototipo no es portable 1:1.** Persiste en `localStorage["fc_instal_v3"]` + IndexedDB, sin
  usuarios ni permisos, y su propio `CLAUDE.md` reconoce deuda (las «Peticiones ciudadanas» viven
  dentro del modelo de Fiestas por no crear claves nuevas). Aquí todo pasa por multi-tenancy, RBAC y
  auditoría: hay decisiones de modelado que el prototipo simplemente no tomó.
- **El prototipo tiene más pantallas** de las que trae este fichero: `plenosModel`, `rutaModel` y
  `fiestasModel.peticiones` aparecen en su `CLAUDE.md` pero viven en otros ficheros del proyecto de
  diseño (`Canvas.dc.html`, `Deck Fuentelcesped.dc.html`). No están cubiertas aquí.
- **Cada endpoint nuevo necesita tests de tenancy y de permisos**; es regla del proyecto, no opcional.
- **Volumen de datos**: las ortofotos y los GeoJSON son megas por municipio. Conviene medir antes de
  elegir dónde viven.

## 6. Cómo retomar

Estado a 2026-07-30: hechas las fases A, B1, B2, B3, B4, B5, B6 y la parte independiente de C4. El cuerpo del Ayuntamiento está completo. Antes, el cascarón (ADR-034, cinco commits en `feat/ayuntamiento-barra-configurable`,
ya en la historia de `feat/despliegue-produccion`) y hecha la **Fase A** (`279e2a5`). Las fases B y C
están sin empezar.

**El cuerpo del Ayuntamiento está terminado**: la barra y los siete epígrafes. De la Fase C está hecha
la parte de C4 que no dependía del mapa.

Lo único que queda es la **cartografía**, y ya no está bloqueada por decisiones de diseño sino por
**topología de ramas**: SIUR la resuelve (§3 bis) pero no está en `main`. El siguiente paso no es
código, es fusionar.

Cuando SIUR esté en `main`, la Fase C se reduce a **C3** (panel de capas, leyenda y búsqueda sobre el
catálogo), **C6** (exportación del recorte) y la parte de C4 que necesita el mapa. Bastante menos de
lo que este documento estimaba al escribirse.
