# Biblioteca de ordenanzas municipales

## Objetivo

La biblioteca convierte el repositorio interno de ordenanzas en una superficie
de consulta jurídica trazable. Permite buscar por significado, abrir la ficha y
el texto indexado de una norma y comparar materias entre municipios. No pretende
declarar por sí sola qué Derecho está vigente: la publicación oficial y el
criterio de los servicios jurídicos siguen siendo la referencia.

La ruta de usuario es `/ordenanzas`. El panel de curación e importación continúa
en `/admin/ordenanzas`.

## Invariantes del corpus

1. Una ordenanza nueva entra en `curation_status=pending_review` y
   `legal_review_status=pending_review`, también cuando se crea manualmente.
   Solo `ordinances.review` puede aprobarla o rechazarla.
2. `curation_status=approved` habilita un registro técnicamente curado para
   recuperación, pero no acredita por sí solo una revisión jurídica humana. La
   recuperación usa además chunks con `review_status=approved`,
   `embedding_status=ready` y el modelo configurado actualmente.
3. `repealed`, `superseded` y `archived` se excluyen de búsquedas, comparación y
   cobertura de Anacleto salvo petición explícita. `unknown` y
   `partially_repealed` siguen siendo recuperables, pero la UI y el prompt deben
   advertir que hace falta comprobar la vigencia.
4. Cambiar texto o metadatos jurídicamente sensibles invalida la curación y la
   revisión jurídica registradas. Si cambia el texto, los chunks derivados
   anteriores se eliminan y se reconstruyen como pendientes. La aprobación
   posterior actualiza también sus estados de revisión.
5. La lista global nunca expone `document_id`. El detalle solo expone los datos
   del documento vinculado cuando el usuario puede acceder a ese documento en su
   organización; en otro caso devuelve ambos campos como `null`.
6. La afinidad vectorial ordena resultados, pero no representa confianza
   jurídica, vigencia ni calidad de la norma. La UI no presenta el score como un
   porcentaje de certeza.
7. Un usuario de consulta no puede enumerar fichas pendientes o rechazadas desde
   el listado administrativo: sin permisos de edición/revisión, el listado queda
   limitado a registros aprobados y un filtro de curación no aprobada responde
   `403`.
8. La recuperación puede usar corpus con curación técnica aprobada y revisión
   jurídica humana pendiente, pero tanto la API como la UI y Anacleto deben
   identificarlo como referencia por validar y remitir a la fuente enlazada.

## Estados jurídicos

| Estado | Recuperación por defecto | Tratamiento de usuario |
| --- | --- | --- |
| `active` | Sí | Sin aviso especial. |
| `partially_repealed` | Sí | Aviso de vigencia parcial y comprobación de artículos. |
| `unknown` | Sí | Aviso de vigencia no verificada. |
| `repealed` | No | Solo aparece con «incluir inactivas» y aviso. |
| `superseded` | No | Solo aparece con «incluir inactivas» y aviso. |
| `archived` | No | Referencia histórica explícita. |

`curation_status` expresa la curación técnica del registro y no sustituye
`status`, que expresa la situación jurídica conocida. Son dimensiones
independientes.

## Curación técnica y revisión jurídica humana

La revisión se registra en dos capas distintas para no convertir una decisión
técnica o automática histórica en una validación jurídica inexistente:

| Campo | Estado | Significado |
| --- | --- | --- |
| `curation_status` | `approved` | Metadatos y fragmentos admitidos en el corpus técnico recuperable. |
| `legal_review_status` | `pending_review` | No consta una decisión jurídica humana vigente. |
| `legal_review_status` | `human_approved` | Una persona con `ordinances.review` aprobó la versión actual; se conservan fecha y revisor. |
| `legal_review_status` | `human_rejected` | Una persona rechazó la versión actual y la ficha queda fuera del corpus recuperable. |

La migración `20260717_0030_ordinance_legal_review_audit.py` deja las filas
históricas en `pending_review` salvo que exista un informe humano vigente,
fechado y coherente con la decisión de curación. No deduce una aprobación humana
desde notas del agente, scores de confianza ni el antiguo estado `approved`. Su
`downgrade` se bloquea si ya existe auditoría humana: no elimina revisor, fecha o
decisión sin una reconciliación explícita previa.

## Consulta semántica

`GET /ordinances/search` es el contrato paginado de la biblioteca. Exige
`ordinances.view` y acepta:

- `q`: consulta obligatoria de 2 a 500 caracteres;
- `municipality_id`, `municipality_name`, `province`;
- `topic` y `strict_topic`; sin modo estricto, la materia aporta un pequeño
  refuerzo al ranking en vez de excluir el resto;
- `population_gte` inclusivo y `population_lt` exclusivo;
- `result_scope=fragments|ordinances|municipalities`;
- `include_inactive`, desactivado por defecto;
- `limit` y `offset`.

La respuesta incluye el total después de aplicar el ámbito, si hay más páginas,
los estados jurídicos excluidos, el estado de revisión humana y la cobertura del
filtro poblacional. `retrieval` declara el modo, modelo, umbral mínimo y cualquier
advertencia de calidad. Un municipio sin población queda fuera cuando se aplica
ese filtro y la respuesta lo hace visible mediante `coverage_complete=false` y
su recuento. Por tanto, cero resultados con filtro poblacional no equivale
necesariamente a ausencia de norma.

PostgreSQL con la extensión `vector` puntúa y pagina el conjunto completo en la
base de datos. El fallback Python conserva la misma semántica para tests o bases
sin la extensión, aunque no es el camino operativo recomendado para corpus
grandes. La migración `20260716_0025_enable_pgvector_search.py` habilita la
extensión. En esta fase el modelo aún conserva la representación textual del
embedding y la consulta la convierte a `vector` para un ranking exacto; no hay
índice ANN. Antes de crecer a un corpus masivo habrá que fijar la dimensión,
migrar la columna a un tipo vectorial y medir HNSW/IVFFlat sin sacrificar la
completitud contractual de la paginación.

El umbral `ORDINANCE_SEARCH_MIN_SIMILARITY` se aplica a la similitud vectorial
cruda antes del refuerzo por materia. Los vectores nulos, sin señal o no finitos
no pueden convertirse en coincidencias. `local_hash` no es semántico: en
desarrollo se limita además a fragmentos que contengan algún término literal
significativo de la consulta y la respuesta devuelve `mode=lexical_hash` con un
aviso. Un entorno de producción debe usar un proveedor semántico aprobado y una
dimensión configurada que coincida exactamente con su respuesta.

### Ámbitos de resultado

- `fragments`: devuelve cada fragmento coincidente; es la vista más exhaustiva.
- `ordinances`: conserva el fragmento mejor puntuado de cada ordenanza.
- `municipalities`: conserva el mejor resultado por municipio lógico, usando el
  código INE y, si falta, la pareja normalizada nombre/provincia.

## Ficha y comparación

`GET /ordinances/{id}` devuelve el texto completo y los metadatos de la ficha.
`GET /ordinances/{id}/chunks` devuelve por defecto únicamente chunks con
curación técnica aprobada; una ficha no aprobada responde `404` por defecto. El
panel de curación puede pedir `include_unreviewed=true`; el detalle exige edición
o revisión y los chunks no curados exigen específicamente
`ordinances.review`. Las normas inactivas también requieren
`include_inactive=true`.

`GET /ordinances/comparison` exige `ordinances.compare`, acepta entre 1 y 20
municipios distintos y devuelve sus metadatos incluso cuando alguno carece de
filas. Así la UI puede distinguir «municipio solicitado sin registro aprobado»
de «municipio no solicitado». `include_pending=true` exige además revisión e
`include_inactive` mantiene la misma política jurídica que la búsqueda.

La comparación es descriptiva: no armoniza conceptos, no resuelve antinomias y
no infiere que la falta de una fila signifique ausencia de regulación.

## Permisos

| Capacidad | Permiso |
| --- | --- |
| Abrir biblioteca, buscar, ficha y chunks curados técnicamente | `ordinances.view` |
| Comparar municipios | `ordinances.compare` |
| Crear borradores | `ordinances.create` |
| Editar contenido | `ordinances.edit` |
| Cambiar estado jurídico y decisión de curación | `ordinances.review` |
| Ver chunks sin curación técnica | `ordinances.review` |
| Archivar | `ordinances.archive` |
| Importar y configurar fuentes | `ordinances.import` |
| Todas las capacidades | `ordinances.manage` |

El panel administrativo no muestra selectores de estado jurídico o revisión a
usuarios que solo pueden editar. Los payloads completos heredados tampoco
provocan una escalada: el backend calcula campos realmente modificados antes de
evaluar permisos.

## Seguridad de importación

Las fuentes siguen una política `official_only`. Crear o modificar una fuente
valida que `base_url` sea HTTP(S), use el puerto estándar, no contenga
credenciales ni fragmento y pertenezca al dominio canónico declarado. No se
admiten dominios que sean IP, `localhost`, etiquetas inválidas o definiciones
incoherentes.

Cada descarga vuelve a validar la URL y el dominio, desactiva proxies, resuelve
DNS una sola vez por conexión y comprueba **todas** las direcciones candidatas.
Se rechazan direcciones privadas, loopback, link-local, multicast, IPv4 mapeada
no pública, 6to4, Teredo y prefijos NAT64 que podrían encapsular destinos
privados. El socket se conecta a la dirección ya validada para evitar DNS
rebinding. Las redirecciones automáticas están deshabilitadas: se siguen
manualmente hasta cinco saltos y se repite la validación en cada destino. Se
mantiene además el límite de bytes descargados.

El conector de descubrimiento BOPBUR no abre red por su cuenta: construye la URL
oficial y recibe obligatoriamente el mismo descargador endurecido. Así la página
de resultados y los PDFs descubiertos comparten límites, validación DNS y
política de redirecciones.

Si un ítem está asociado a `official_source_id`, su URL y todas sus redirecciones
deben pertenecer a esa fuente concreta, no solo a cualquier fuente activa del
trabajo.

## Fragmentación e indexación

La fragmentación respeta artículos y disposiciones cuando existen marcadores y
divide también párrafos individuales que superen el tamaño configurado. Si el
texto produciría más de `ORDINANCE_IMPORT_MAX_CHUNKS`, la operación falla de
forma atómica: nunca se conserva solo el prefijo de una ordenanza ni se eliminan
los chunks anteriores antes de validar el reemplazo. El valor de referencia es
500; debe dimensionarse con datos reales, no reducirse truncando texto. Los
fragmentos que solo contienen puntuación se descartan y nunca generan vectores
cero recuperables.

Altas, ediciones e importaciones guardan primero los chunks derivados con estado
de embedding explícito. `local_hash` se calcula después del commit por ser local
y determinista; un proveedor `openai_compatible` crea un trabajo RQ idempotente
por chunk, con timeout acotado y hasta tres reintentos, fuera de la petición HTTP y sin
mantener una transacción de base de datos durante la llamada de red. Si la cola
no está disponible o un worker termina parcialmente, los chunks permanecen
`pending`/`failed` y, por tanto, fuera del corpus recuperable. Un operador con
`ordinances.import` puede reencolarlos con
`POST /ordinances/{id}/retry-embeddings`; el reintento de cobertura Burgos
también encola cuando el proveedor es remoto. La migración
`20260716_0026_default_ordinances_to_pending_review.py` cambia únicamente el
valor por defecto de futuras altas y no reclasifica decisiones existentes.

La creación de ordenanza, chunks e informe de una importación se confirma como
una sola unidad. Un fallo intermedio revierte esos derivados, marca el ítem como
fallido y permite reejecutarlo sin convertirlo en un falso duplicado. Los ítems
marcados como `duplicate` son trazabilidad, no una vía de revisión: solo el ítem
`pending_review` propietario de la ordenanza puede aprobarla o rechazarla.
Reejecutar un job completado conserva los ítems pendientes, aprobados, rechazados
o duplicados ya vinculados; para pendientes y aprobados solo recupera de forma
idempotente los embeddings que sigan sin terminar.

Los informes de revisión se versionan con el contenido. Una edición sensible
marca el informe anterior como `superseded` y crea un checklist nuevo sobre la
versión vigente. Pedir cambios también cierra el informe actual: no se puede
aprobar otra vez hasta que una nueva edición haya generado un informe revisable.

## Auditoría y reparación supervisada

La auditoría es de solo lectura y produce un plan determinista. Compara todos los
chunks guardados con el fragmentador vigente, detecta fragmentos inválidos o
sobredimensionados, límites excedidos, embeddings inválidos/obsoletos/sin
terminar, texto o fuentes ausentes, títulos de aprobación inicial/provisional,
fechas ausentes y revisión jurídica pendiente:

```bash
cd backend
python -m app.ordinances.corpus_maintenance audit
python -m app.ordinances.corpus_maintenance audit --ordinance-id 123
```

El JSON incluye huellas del registro, del texto derivado esperado y de la
estructura de chunks guardada, además de `plan_sha256`. Reparar exige seleccionar
expresamente cada ficha y confirmar exactamente ese hash; cualquier cambio entre
auditoría y reparación detiene la operación:

```bash
python -m app.ordinances.corpus_maintenance repair \
  --ordinance-id 123 \
  --expected-plan-sha256 <sha256-confirmado>
```

La reparación solo reconstruye derivados incoherentes. Nunca autoaprueba el
resultado: reabre la curación, borra la aprobación jurídica anterior, deja los
chunks pendientes y genera un nuevo informe cuando la ficha procede de una
importación. Después hacen falta revisión humana, reindexación completada y una
nueva auditoría antes de devolverla al corpus recuperable. Los problemas
exclusivamente de embedding se informan como no resueltos: deben corregirse con
el endpoint de reintento/reindexación después de revisar modelo, dimensión y
proveedor, sin reabrir innecesariamente el contenido jurídico.

La auditoría global añade un bloque `import_failures` separado. Agrupa por URL
exacta y considera resuelto un fallo solo cuando existe un ítem posterior con
ordenanza enlazada en `approved`, `pending_review` o `duplicate`. Conserva el
detalle de URLs aún sin resolver, host, error y jobs, y genera su propio
`import_failure_snapshot_sha256`; un reintento concurrente no invalida el plan de
reparación de chunks. Esta CLI no reintenta descargas. Crear nuevos jobs y
corregir fuentes requiere revisión del manifiesto y el lease del runtime; las
filas fallidas históricas no se reescriben ni se eliminan.

## Estado en URL y accesibilidad

La biblioteca persiste consulta, filtros, página, ficha (`id`) y municipios de
comparación (`compare`) en la URL. Esto soporta enlaces profundos, recarga y
navegación atrás/adelante. La ficha del ayuntamiento enlaza a la biblioteca con
`municipality_id` preseleccionado.

Resultados, ficha y comparación usan encabezados y tablas semánticas, estados de
carga anunciables, foco visible, controles con nombre accesible y enlaces
externos seguros. La tabla comparativa admite desplazamiento horizontal y la
ficha deja de ser lateral en pantallas estrechas.

## Comprobación operativa

Antes de considerar utilizable un corpus:

1. comprobar fuentes oficiales activas y coherentes;
2. ejecutar la auditoría del corpus y conservar su resumen y `plan_sha256`;
3. revisar fallos de descarga, extracción, OCR, URLs ausentes y anuncios
   iniciales o provisionales;
4. distinguir curación técnica de revisión jurídica humana y priorizar una
   muestra de `legal_review_status=pending_review`;
5. comprobar que los chunks están aprobados técnicamente, `ready`, contienen
   texto útil y pertenecen al modelo vigente;
6. usar un proveedor semántico aprobado en producción; `local_hash` solo sirve
   como ayuda léxica de desarrollo;
7. ejecutar búsquedas positivas y negativas con municipio/materia;
8. probar la exclusión de normas inactivas y la advertencia de estado
   desconocido/parcial;
9. verificar una comparación con un municipio sin cobertura;
10. contrastar una muestra contra el boletín oficial y leer completos los
    fragmentos truncados relevantes.

La cobertura que Anacleto recibe en su prompt cuenta las ordenanzas técnicamente
recuperables y declara por separado cuántas tienen revisión humana aprobatoria.
Si no hay cobertura, el asistente debe decirlo en vez de inventar normativa.
