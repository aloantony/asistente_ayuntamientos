# Diseño: pantalla Ayuntamiento e Instalaciones (prototipo de Claude Design)

Actualizado: 2026-07-30. Plan por fases, **pendiente de decisiones abiertas** (§3).

Fuente: proyecto de Claude Design `bb6236ef-e522-46db-a8ce-2f9097b2a3d1`, fichero
`uploads/ZIP del  HTML de claude Design/Pantalla Principal.dc.html` (1.437 líneas, tres pantallas:
Inicio, Ayuntamiento e Instalaciones). Copia local del HTML:
`~/.claude/plans/assets/pantalla-principal.dc.html`.

Este documento no describe el estado implementado —para eso están `arquitectura.md` y `requisitos.md`—
sino **lo que falta para que la aplicación se parezca al prototipo**, en qué orden y con qué decisiones
previas. Es el equivalente para esta pantalla de lo que `diseno-dialogo-voz.md` fue para la voz.

## 1. Punto de partida

Lo ya construido y reutilizable:

- **`municipal_blocks` / `municipal_profiles`** (ADR-030): árbol genérico de bloques por organización
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
- **`geo`**: **solo puntos**. `GeoLocation` (punto, lat/lon, municipio, organización) y
  `EntityLocation` (`requirement|project`). No hay capas, ni GeoJSON almacenado, ni WMS, ni polígonos.
  `MunicipalMap` es un envoltorio de Leaflet con teselas de OpenStreetMap.

## 2. Inventario: prototipo frente a implementado

### Barra del municipio
| Elemento del prototipo | Estado |
|---|---|
| Escudo 46×56, reemplazable arrastrando una imagen | Hecho |
| Nombre del municipio en Newsreader 24px | Hecho |
| Bloque de temperatura del día | Hecho (Open-Meteo, ADR-030) |
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
4. **Gráficas.** Padrón, clima y agua necesitan series temporales y dibujo. El proyecto no tiene
   librería de gráficas y su convención es «sin librerías de UI». Decidir: SVG propio (coherente pero
   más trabajo) o introducir una dependencia con ADR.
5. **Modelo de capas cartográficas.** Dos caminos: (a) tabla de capas + features con GeoJSON en
   PostgreSQL; (b) capas como ficheros GeoJSON en el volumen de almacenamiento, con metadatos en
   PostgreSQL. **Recomendación: (b)** — son ficheros grandes y estáticos, y evita meter geometría en
   una base sin PostGIS en la ruta de esta rama.
6. **Mapas base externos** (ortofoto PNOA, catastro, IDECyL). Son WMS de terceros consumidos **desde
   el navegador**: es un egreso nuevo y necesita ADR, igual que lo necesitó Open-Meteo. Hoy solo
   salen teselas de OpenStreetMap.
7. **El `geoserver` huérfano.** Hay un contenedor `geoserver` corriendo que **no está en el
   `docker-compose.yml` de esta rama**: viene de otra rama `codex/*`. Decidir si se adopta como
   servidor de capas propias (lo que resolvería el punto 5) o se descarta.
8. **Los 40+ GeoJSON de Fuentelcésped** (`assets/carto/` del proyecto de diseño: límite, parcelas,
   edificios, hidrografía, urbanismo, energías, yacimientos…) más ortofotos. Ya se decidió importarlos
   como datos reales del municipio piloto; falta decidir **dónde viven**: son varios MB de un
   municipio concreto en un producto multi-tenant. Recomendación: volumen de almacenamiento, nunca el
   repositorio.
9. **Numeración de ADR.** El último es el 033. Los ADR de estas fases arrancan en el **034**.

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
- **B5 Datos del municipio** — PARCIAL. Hechos (`c06666d`) el formato `data` (rejilla de pares
  dato/valor) para la pestaña General, y la vista previa de las imágenes adjuntas, que es lo que
  necesitaban Patrimonio y la Fototeca. **Pendientes Demografía, Clima y Agua**, que necesitan
  gráficas: *no cerrar sin la decisión 4*. El padrón puede apoyarse en `municipalities.population` y
  su procedencia INE, ya existentes.
- **B6 Normativa** — vista de categorías y documentos **sobre el módulo `ordinances` existente**, no
  un almacén nuevo.

### Fase C — Instalaciones (la mayor)
- **C1 Modelo de capas** — metadatos en PostgreSQL, geometría según la decisión 5, con permisos
  propios (`map.import` ya está sembrado y hoy no tiene endpoint), tenancy y tests.
- **C2 Importación de los GeoJSON de Fuentelcésped** — *cierra la decisión 8*.
- **C3 Panel de 5 pestañas, leyenda y búsqueda** — sobre `MunicipalMap`.
- **C4 Fichas y mantenimiento** — vista del prototipo **consumiendo `assets` y `maintenance`**, con
  sus filtros de vencimiento. Es la sub-fase con mejor relación resultado/esfuerzo de la Fase C.
- **C5 Mapas base y ortofotos** — *cierra las decisiones 6 y 7*, con su ADR de egreso.
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

Estado a 2026-07-30: hechas las fases A, B1, B2, B3, B4 y media B5. Antes, el cascarón (ADR-030, cinco commits en `feat/ayuntamiento-barra-configurable`,
ya en la historia de `feat/despliegue-produccion`) y hecha la **Fase A** (`279e2a5`). Las fases B y C
están sin empezar.

Al retomar, dos caminos posibles:
- **B6 (Normativa)**, que no necesita decisiones nuevas: es una vista sobre el módulo `ordinances` que
  ya existe. Es el último epígrafe que se puede hacer sin abrir nada.
- **Cerrar la decisión 4** y terminar B5 con Demografía, Clima y Agua.

Después de eso solo queda la **Fase C (Instalaciones)**, con sus cinco decisiones abiertas (5 a 8).
