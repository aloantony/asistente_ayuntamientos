# Diseño: pantalla Ayuntamiento e Instalaciones (prototipo de Claude Design)

Actualizado: 2026-09-04. Cuerpo del Ayuntamiento terminado; la cartografía cambió de rumbo (§3 bis, ADR-064).

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
- **Cartografía**: un JSON estático por municipio, dibujado con Leaflet. El espejo SIUR que
  ocupaba este hueco se retiró; ver §3 bis y ADR-064.

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
por arrastre. **La tarjeta —plegado, arrastre y menú— está hecha (§9); los contenidos, en la Fase B.**

> **Corregido en §10 (2026-08-31)**: la tabla de abajo se escribió con la copia truncada y *no* son
> los siete epígrafes del diseño. Los de verdad están en su `infoDefault`. Se conserva porque el
> inventario de contenidos sigue valiendo; la lista buena es la de §10.

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
   respondía SIUR; hoy las responde ADR-064. Ver §3 bis.
9. **Numeración de ADR.** El último es el 033. Los ADR de estas fases arrancan en el **034**.

## 3 bis. La cartografía: SIUR primero, un fichero después (2026-09-04)

Este apartado decía que la cartografía «ya existía» en el módulo
`backend/app/reference_layers/` —más de setenta ficheros, un GeoServer propio, un
GeoWebCache, un espejo del catálogo SIUR— y que la Fase C dependía de fusionarlo.
Se fusionó, se desplegó y se usó durante dos meses.

**Se ha retirado entero.** Aquella máquina resolvía la reutilización soberana de la
cartografía pública de una comunidad autónoma; lo que el producto necesita es que un
pueblo vea sus calles. El mapa se dibuja ahora desde un JSON estático por municipio
(Catastro para los edificios, OpenStreetMap para lo demás), sin servidor de mapas de
ninguna clase. Los motivos y lo que se pierde están en **ADR-064**; el funcionamiento,
en `docs/mapa-municipal.md`.

Lo que sigue valiendo de este apartado es la advertencia que lo motivó: mirar qué hay
construido antes de construir. Lo que ya no vale es su conclusión.

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

### Fase C — Instalaciones (sobre el plano del municipio)
- **C1 Modelo de capas** — **HECHA por otra vía** (ADR-044): el árbol de capas se deriva de lo que hay
  situado en el mapa, no de un catálogo externo.
- **C2 Cartografía de Fuentelcésped** — **HECHA** (ADR-064): edificios del Catastro y viales de
  OpenStreetMap en un JSON estático que viaja con la aplicación.
- **C3 Panel de capas, leyenda y búsqueda** — hecha su parte de capas y búsqueda en
  Ayuntamiento → Mapa general, derivada de los propios elementos.
- **C4 Fichas y mantenimiento** — HECHA (`305e43c`) en su parte independiente: filtro de vencimiento
  (todas / ≤30 días / vencidas) resuelto **en el servidor** con `scheduled_to`, y aviso en rojo de las
  órdenes pasadas de fecha. Consume `assets` y `maintenance`, sin almacén propio. Queda pendiente lo
  que sí depende del mapa: los paneles por capa y la búsqueda de elementos sobre la cartografía.
- **C5 Mapas base y ortofotos** — **FUERA DE ALCANCE** (ADR-064): no hay mapa base externo ni
  ortofoto; el fondo es el propio plano del municipio, y el navegador no sale a ningún servidor.
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

La **cartografía** ya está: el plano de Fuentelcésped se dibuja desde un fichero propio (ADR-064) y
el mapa general lo usa con las capas derivadas del inventario.

De la Fase C quedan **C6** (exportación del recorte) y la parte de C4 que necesita el mapa: los
paneles por capa y la búsqueda de elementos sobre la cartografía. Bastante menos de lo que este
documento estimaba al escribirse, y por un camino distinto del que preveía.

## 7. GIRO (2026-08-02): el diseño no tiene pestañas fijas

Al revisar la pantalla ya fusionada, Anthony señaló que no se parece al prototipo. Al volver al
proyecto de Claude Design aparecieron dos cosas que invalidan parte de lo escrito arriba.

### 7.1 El HTML del que se trabajó estaba truncado

La copia durable (`~/.claude/plans/assets/pantalla-principal.dc.html`) pesa exactamente 262.144
bytes —el tope de 256 KiB de `DesignSync.get_file`—, termina a mitad de etiqueta y no tiene
`</html>`. Todo lo implementado hasta ahora salió de un diseño incompleto. `get_file` devuelve un
campo `truncated`; conviene comprobarlo siempre antes de dar por buena una lectura.

### 7.2 Estructura real de la pantalla «Ayuntamiento»

```
<div data-screen-label="Ayuntamiento"
     style="display:flex;flex-direction:column;gap:24px;max-width:1440px;margin:0 auto;">

  <!-- fila de pestañas CONFIGURABLES, no fijas -->
  <div style="display:flex;flex-wrap:wrap;gap:4px;
              border-bottom:1px solid var(--border);padding-bottom:10px;">
    <button title="Gestionar pestañas" 28x28 border:0 radius:8
            background:transparent color:var(--text-faint)
            hover: background:var(--fill) color:var(--text)>
    <for aySecTabs as t>
      <button style="border:0;border-radius:8px;padding:7px 13px;
                     background:{t.bg};color:{t.col};
                     font-size:12.5px;font-weight:600;">{t.label}</button>
  </div>

  <!-- epígrafes de la pestaña activa, APILADOS -->
  <article style="max-width:900px;display:flex;flex-direction:column;gap:12px;">
    ... una tarjeta por epígrafe ...
  </article>
</div>
```

Tarjeta de epígrafe (`epiList.<clave>`), con `order` y `display` controlados desde el modelo:

- contenedor: `border:1px solid var(--border); border-radius:13px; background:var(--surface);
  padding:0 18px; position:relative`, más `onDragOver`/`onDrop`.
- cabecera: `display:flex; align-items:center; gap:12px`.
  - asa `draggable`: `color:var(--text-faint); cursor:grab; padding:3px; border-radius:5px`,
    hover `color:var(--text); background:var(--fill)`; SVG 14×14 con seis círculos `r=1.6`
    en (9|15, 6|12|18). El clic abre el menú del epígrafe.
  - zona de título `draggable`, clic = plegar: `flex:1; min-width:0; display:flex;
    align-items:center; justify-content:space-between; gap:16px; padding:16px 0; cursor:pointer`.
    - `<h2>`: `margin:0; font-size:18px; font-weight:600; letter-spacing:-0.01em`.
    - chevron 18×18 `m6 9 6 6 6-6`, `stroke:var(--text-muted)`, grosor 2, gira al plegar.
  - menú: `position:absolute; left:26px; top:56px; z-index:50; min-width:190px; padding:6px;
    border:1px solid var(--border); border-radius:10px; background:var(--surface-raised);
    box-shadow:var(--shadow-raised)`. Opciones: **Renombrar**, **Añadir nivel**,
    **Eliminar epígrafe**. Cada una `padding:8px 10px; border-radius:7px; font-size:13px`,
    hover `background:var(--bg)`.

Siete epígrafes en el diseño: `estructura`, `corp`, `org`, `normativa`, `datos`, `telefonos`,
`archivo`.

### 7.3 Qué hay que cambiar

`MunicipalWorkspace` sirve hoy **cinco pestañas fijas** —Resumen, Normativa, Instalaciones,
Personal, Hoja de ruta— que no salen del diseño: las introdujo otra rama `codex/*`. «Hoja de ruta»
no aparece ni una sola vez en el HTML del prototipo. Decisión de Anthony (2026-08-02):
**reestructurar al diseño**, conservando dentro de las tarjetas el contenido real que hoy vive en
esas pestañas, para no perder funciones conectadas al backend.

`isInstalaciones` es el **mapa incrustado**, con `height:min(720px, calc(100vh - 250px))` y las
cinco agrupaciones de capas `isBase`, `isTerritorio`, `isUrbanismo`, `isInfra`, `isAnalisis`.
Hoy la pestaña solo enlaza a `/mapa`.

## 8. CORRECCIÓN (2026-08-03): el diseño completo llegó por GitHub

Anthony subió el proyecto exportado a la rama `agent/add-fuentelcesped-design-export`:
`design/exports/Fuentelcesped - Pantalla Principal (standalone).html`, **9,4 MB**. El standalone
lleva la plantilla `.dc` incrustada como literales JavaScript; desescapada quedan 9,3 MB. La copia
con la que se trabajó hasta ahora eran 262.144 bytes: el **2,8 %** del diseño.

Copia durable desescapada: `~/.claude/plans/assets/pantalla-principal-COMPLETA.dc.html`.

### 8.1 Lo que §7 acertó

La estructura de la pantalla «Ayuntamiento» es exactamente la descrita en §7.2: fila de pestañas
configurables (`aySecTabs`) con el botón «Gestionar pestañas» delante, y debajo los epígrafes
apilados en tarjetas arrastrables. Las medidas de §7.2 son correctas y siguen valiendo.

### 8.2 Lo que §7 se inventó por leer solo el 2,8 %

**«Hoja de ruta» sí está en el diseño.** §7.3 afirma que no aparece «ni una sola vez»: falso. Es
una pantalla propia, `isRuta`, con su `data-screen-label="Hoja de ruta"`.

Y no está sola. El diseño tiene **trece pantallas de primer nivel**, no tres:

| Bandera | Pantalla |
|---|---|
| `isInicio` | Inicio |
| `isAyuntamiento` | Ayuntamiento |
| `isInstalaciones` | Instalaciones (mapa + capas) |
| `isAdmon` | Administración |
| `isPersonalModule` | Módulo Personal |
| `isRuta` | Hoja de ruta |
| `isSede` | Sede electrónica |
| `isNecesidades` | Necesidades |
| `isMapa` | Mapa |
| `isProyectos` | Proyectos |
| `isAnacleto` | Anacleto |
| `isCuenta` | Mi cuenta |
| `isGroupView` | Vista de grupo (dentro de Instalaciones) |

Dentro de Instalaciones, las agrupaciones de capas son seis, no cinco: `isBase`, `isTerritorio`,
`isUrbanismo`, `isInfra`, `isAnalisis` y `isPatrimonio`.

Secciones marcadas con banner en el fichero: INICIO · BARRA SUPERIOR GLOBAL (ayuntamiento + ruta) ·
AYUNTAMIENTO · FICHAS Y MANTENIMIENTO (paneles bajo el mapa) · FICHA URBANÍSTICA (recinto) ·
FICHA TÉCNICA DEL ELEMENTO · CONFIGURACIÓN (reventa a otros ayuntamientos) · MÓDULO PERSONAL ·
DASHBOARD: CONTROL PERSONAL · FICHA DEL TRABAJADOR · CONTROL DE ASISTENCIA · HOJA DE RUTA ·
SEDE ELECTRÓNICA · NECESIDADES · MAPA · PROYECTOS · ANACLETO · MI CUENTA.

### 8.3 Consecuencia para las cinco pestañas

`MunicipalWorkspace` sirve Resumen · Normativa · Instalaciones · Personal · Hoja de ruta como
pestañas *dentro* del Ayuntamiento. En el diseño, **Instalaciones, Personal y Hoja de ruta son
pantallas de primer nivel**, hermanas del Ayuntamiento, no pestañas suyas. Las pestañas del
Ayuntamiento son las configurables `aySecTabs`.

Así que el diagnóstico de §7.3 —«las cinco pestañas no salen del diseño»— era medio falso: los
nombres sí salen, lo que está mal es **el nivel de navegación en el que viven**.

### 8.4 El menú del epígrafe tiene cuatro opciones, no tres

§7.2 leyó **Renombrar · Añadir nivel · Eliminar epígrafe**. En el diseño completo son
**Renombrar · Subir · Bajar · Eliminar epígrafe**: no hay «Añadir nivel», y sí hay movimiento
arriba/abajo, que es la alternativa por teclado al arrastre. La geometría del menú que §7.2
describe (`left:26px; top:56px`, ancho mínimo 190, opciones de 13 px) sí es correcta.

El chevron de plegado gira con `transform:rotate(180deg)` y `transition:transform .18s ease`.

## 9. Tarjetas de epígrafe (2026-08-03) — HECHO

Implementadas las tarjetas del cuerpo del Ayuntamiento, que es lo que §8 identifica como la
estructura real de la pantalla. **Sin cambios de backend ni de esquema**: el árbol de
`municipal_blocks` ya tenía la forma necesaria, solo estaba mal proyectado en la pantalla.

**La corrección de nivel.** El árbol se lee ahora como lo lee el diseño:

| Bloque | Antes | Ahora |
|---|---|---|
| `nav_section` | pestaña con desplegable al hover | **pestaña** (`aySecTabs`), plana, sin desplegable |
| `nav_item` | opción del desplegable, con panel propio | **tarjeta de epígrafe**, apilada bajo la pestaña |
| `item` | elemento del panel | elemento dentro de la tarjeta, sin cambios |

La fila de pestañas del diseño son botones planos: no lleva desplegables. Los tenía la
implementación de la Fase A, no el prototipo.

**Lo que hace cada tarjeta**: plegarse (chevron), arrastrarse para reordenar y abrir su menú desde
el asa, con las cuatro opciones de §8.4. Renombrar convierte el `<h2>` en un campo que guarda al
perder el foco o con Intro, y Escape descarta —decisión 2, nada de `contenteditable`—. Eliminar
pasa por `ConfirmDialog`, como el resto de acciones destructivas.

**Contenido por tarjeta.** `useTownHallController` guardaba **un** contenido; ahora guarda un mapa
`contents[blockId]` y una lista de los que están en vuelo, porque varias tarjetas pueden estar
abiertas a la vez. Cada tarjeta abierta pide el suyo; las plegadas no piden nada. Al entrar en una
pestaña se despliega su primer epígrafe: abrirla con todo plegado no enseñaría nada. Un fallo de
carga deja esa tarjeta —y solo esa— con su botón de reintento.

**Enlaces antiguos.** `?tab=block-<id>` apuntando a un epígrafe se traduce a la pestaña que lo
contiene, con esa tarjeta desplegada, y la URL se reescribe. Qué tarjetas están abiertas es estado
de presentación y no viaja en la URL; la pestaña sí, como hasta ahora.

**Vocabulario.** El editor pasa a llamar a las cosas como el diseño: `nav_section` es «pestaña» y
`nav_item` es «epígrafe» (antes «apartado» y «elemento», que además chocaba con los `item`).

Lo que **no** hace esta fase, y sigue abierto de §8.3: sacar Instalaciones, Personal y Hoja de ruta
del Ayuntamiento a pantallas de primer nivel. Son rutas nuevas, y hoy conviven como pestañas fijas
delante de las configurables.

### 9.1 El Ayuntamiento configurable nunca se había llegado a cargar

Al revisar la pantalla con datos sembrados no aparecía ninguna pestaña. La causa no eran las
tarjetas: `MunicipalWorkspace` guardaba la organización elegida en `selectedOrganizationId`,
inicializado a `null` y escrito **solo** por el selector de organización, que a su vez solo se pinta
cuando el usuario pertenece a más de una. Con una sola organización —el caso de Fuentelcésped y el
de cualquier ayuntamiento real— el estado se quedaba a `null` para siempre, así que la condición de
guarda cortaba `loadTownHall()` y el controlador se construía con `organizationId: 0`.

Consecuencia: **escudo, nombre configurable, temperatura, pestañas y epígrafes no se cargaban
nunca** para un usuario de una sola organización. El fallo venía del commit original del módulo
(`1af5ac9`), no de esta fase; estaba tapado porque hasta ahora no había datos que enseñar.

Arreglado derivando la organización activa de `selectedContext` —que ya caía en la primera cuando
no se ha elegido ninguna— en vez de del estado. El estado se conserva para la elección explícita
del selector.

### 9.2 Comprobado en el navegador

Con una pestaña «Información» y los siete epígrafes del diseño, sobre Chromium:

- Las siete tarjetas se apilan, la primera desplegada y las demás plegadas.
- El menú del asa trae las cuatro opciones de §8.4.
- «Bajar» reordena y **el orden persiste tras recargar**.
- Renombrar guarda con Intro; plegar y desplegar deja dos tarjetas abiertas a la vez.
- `?tab=block-<id>` entra en la pestaña con su primera tarjeta abierta.
- Claro y oscuro, ambos correctos.

El botón «Gestionar pestañas» pasa a usar el asa de seis puntos del prototipo; llevaba un
engranaje, que no sale del diseño.

## 10. La estructura de partida se siembra (2026-08-31) — HECHO

El cuerpo del Ayuntamiento estaba terminado desde §9, pero un ayuntamiento real abría la pantalla y
encontraba la fila de pestañas vacía. Se añade un seed por organización y bajo petición
(`backend/app/town_hall/seed.py`, `POST /town-hall/structure/seed`), con el patrón de ADR-043. La
decisión está en **ADR-053**.

### 10.1 Los siete epígrafes de §2 no eran los del diseño

`infoDefault` del proyecto exportado es, en este orden:

```
["estructura", "datos", "suministros", "normativa", "archivo", "telefonos", "org"]
```

y sus títulos, de `infoDefTitles`: **Estructura de Gobierno · Datos del municipio · Suministros ·
Normativa municipal · Archivo municipal · Teléfonos de interés · Organismos y empresas**.

La tabla de §2 daba otros siete: colaba «Información general» y «Corporación municipal» como
epígrafes de primer nivel —son una pestaña interna de *Datos del municipio* y una sección de
*Estructura de Gobierno*— y se dejaba fuera «Suministros» y «Organismos y empresas». Es el mismo
error que §8 corrigió en otros puntos: se leyó el 2,8 % del fichero.

### 10.2 Cuatro pestañas, dieciséis apartados

El diseño tiene cuatro niveles (pestaña → epígrafe → pestaña interna → contenido) y el modelo tres
(pestaña → apartado → elemento). Se colapsa el del epígrafe, porque cada pestaña interna suya trae un
formato distinto y un apartado sólo admite uno:

| Pestaña | Apartados (formato) |
|---|---|
| Información del municipio | Estructura de Gobierno (`people`), Corporación Municipal (`people`), Suministros (`series`), Organismos y empresas (`text`) |
| Datos del municipio | Información general (`data`), Datos demográficos (`series`), Registro climatológico (`series`), Análisis de agua potable (`files`), Patrimonio (`data`) |
| Archivo municipal | Archivo (`files`), Fototeca (`files`), Crónicas (`text`), Himno (`text`) |
| Teléfonos de interés | Servicios e instituciones (`contacts`), Equipo de gobierno (`contacts`), Personal municipal (`contacts`) |

**Normativa municipal se deja fuera**: ya existe como biblioteca de ordenanzas y como área fija, y
sembrarla aquí bifurcaría el dominio (fase B6).

El seed **no crea contenido municipal**: ni teléfonos, ni concejales, ni padrón. Sólo el esqueleto.
