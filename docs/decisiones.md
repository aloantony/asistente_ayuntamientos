# Decisiones técnicas

## ADR-001: Separación backend/frontend

Se mantiene una separación clara entre API FastAPI y aplicación Next.js para permitir evolución independiente de cada parte.

## ADR-002: Docker Compose para desarrollo local

Se usa Docker Compose como entorno local inicial porque permite levantar backend, frontend, PostgreSQL y Redis de forma reproducible.

## ADR-003: Configuración por variables de entorno

El backend lee la configuración desde variables de entorno para evitar valores acoplados al código y facilitar futuros despliegues.

## ADR-004: Alcance mínimo inicial

La primera versión mantiene el alcance acotado a la base técnica, autenticación y un modelo RBAC inicial. No incluye integraciones externas ni automatizaciones avanzadas.

## ADR-005: RBAC simple como base inicial

Se usa un modelo RBAC explícito y sencillo: usuarios, grupos, roles y permisos. La pertenencia se encadena como usuario-grupo, grupo-rol y rol-permiso, con un indicador `is_superuser` para el propietario inicial.

Esta decisión evita introducir todavía un motor complejo de políticas. Para la fase inicial necesitamos una estructura fácil de entender, migrar y auditar. La comprobación fina de permisos se podrá añadir después sobre estas tablas cuando existan casos de uso reales.

## ADR-006: Endurecimiento del aislamiento multi-tenant (2026-06-12)

Una auditoría del código detectó vías de escalada entre tenants. Se decide:

- Conceder o retirar `is_superuser` es operación exclusiva de superusuarios (antes bastaba `users.manage`).
- `users.manage` queda delimitado por organización: solo se pueden editar/borrar usuarios que comparten alguna organización donde el administrador tiene el permiso.
- La guarda de "último superusuario activo" se aplica también a PATCH (desactivación/degradación), no solo a DELETE.
- Roles y permisos son objetos globales de plataforma: sus mutaciones quedan reservadas a superusuarios. La asignación grupo-rol sigue delimitada por la organización del grupo.
- Crear organizaciones (tenants nuevos) queda reservado a superusuarios.
- Enlazar un documento a una ordenanza exige que el autor tenga acceso a ese documento; los documentos inaccesibles responden 404 para no filtrar su existencia.

Contexto: el primer usuario externo (alcalde, usuario no superusuario) entra pronto; el modelo anterior asumía operador único de confianza.

## ADR-007: Seeding automático del catálogo de permisos (2026-06-12)

El backend siembra el catálogo de permisos de forma idempotente al arrancar (hook lifespan). Antes requería una llamada manual de superusuario y una base nueva quedaba con la tabla vacía. Si las migraciones no se han aplicado aún, el arranque continúa con un warning para permitir ejecutar Alembic.

## ADR-008: Arnés de tests con pytest en contenedor (2026-06-12)

Los tests de backend corren con pytest dentro del contenedor backend (mismas versiones que producción), contra una base PostgreSQL de test separada. Cada test se envuelve en una transacción externa con savepoints (`join_transaction_mode="create_savepoint"`) y rollback final: los `commit()` del código de aplicación funcionan y ningún test toca datos de desarrollo. Las dependencias de test viven en `requirements-dev.txt` y no entran en la imagen.

Prioridad de cobertura: matriz de permisos, aislamiento entre tenants y reglas de escalada, por ser el código cuya regresión es más cara y silenciosa.

## ADR-009: Agente de IA de intake de requisitos antes que Ordenanzas v1 (2026-06-12)

El primer usuario real es un alcalde que comunicará los requisitos del producto conversando con un agente de IA ("tipo Jarvis"). Se reordena el roadmap: el agente conversacional de intake va antes que Comparación de Ordenanzas v1. Principios: el agente opera con los permisos RBAC del usuario, crea requisitos como borradores supervisables, deja rastro auditable de cada acción, y toda llamada a la API externa de IA pasa por un gateway interno con minimización de datos (sin enviar documentos originales).

## ADR-010: Sesión de navegador con cookie httpOnly (2026-06-12)

El frontend deja de guardar el JWT en localStorage (expuesto a XSS). El login emite una cookie `access_token` httpOnly SameSite=Lax (Secure fuera de desarrollo) y existe `POST /auth/logout` para limpiarla. La cabecera `Authorization: Bearer` sigue aceptada para clientes de API y tests. SameSite=Lax mitiga CSRF en los POST cross-site; frontend y backend deben servirse desde el mismo host (localhost) para compartir la cookie entre puertos. Se añade rate limiting en memoria a `POST /auth/login` (10 intentos/minuto por IP; al escalar a multi-worker deberá moverse a Redis).

## ADR-011: Paginación server-side con X-Total-Count (2026-06-12)

Los listados de municipios, ordenanzas, requisitos y usuarios admin aceptan `limit` (1-200, por defecto 100) y `offset`, y devuelven el total en la cabecera `X-Total-Count` (expuesta vía CORS). Se eligió cabecera en lugar de envolver la respuesta para no romper los contratos existentes. El listado de ordenanzas ya no incluye `text_content`: el texto legal completo solo viaja en el detalle, lo que desbloquea importar el dataset INE (~8.100 municipios) sin respuestas gigantes.

## ADR-012: Dictado por voz solo con reconocimiento local (2026-06-12)

El asistente acepta entrada por voz mediante la Web Speech API **exclusivamente en modo local** (`processLocally = true`, Chrome 139+): el audio no sale del equipo del usuario. El modo nube por defecto del navegador envía el audio a servidores del proveedor sin DPA que cubra ese tratamiento — inaceptable para datos de una administración pública — así que cuando el reconocimiento local no está disponible el botón se deshabilita en lugar de degradar. Alternativa futura con mejor posición RGPD si hace falta cobertura universal: Whisper auto-alojado (ver docs/investigacion-api-ia.md §6).

## ADR-013: Criterio de selección del proveedor de IA (2026-06-12)

La investigación comparativa (docs/investigacion-api-ia.md) concluye: candidato principal Mistral Small 4 (procesamiento UE por defecto, ~$0,10/$0,30 por MTok, sujeto a una evaluación de fiabilidad de tool use con ~50 conversaciones reales); segunda opción Claude Haiku 4.5 vía Bedrock con endpoints UE (tool use más fiable, `strict`, caché). El coste del LLM no es el criterio decisivo a los volúmenes previstos ($75–950/mes en el escenario alto): pesan más la fiabilidad de herramientas y el encaje RGPD/ENS. El gateway mantiene el modelo configurable (`ASSISTANT_MODEL`); la migración de proveedor, si se decide, se limita a `app/assistant/gateway.py`. Reevaluar el 2026-07-01 (subidas de precio UE anunciadas) y al incorporar las cargas futuras de ordenanzas.

## ADR-014: Frontend multi-ruta con shell de navegación (2026-06-12)

La página única con cinco paneles apilados se sustituye por rutas del App Router con una barra lateral cuyos ítems usan los mismos predicados de permisos que gobernaban los paneles: `/asistente`, `/requisitos`, `/proyectos`, `/cuenta` y `/admin/{usuarios,grupos,organizaciones,roles,municipios,ordenanzas}`. Decisiones asociadas:

- La sesión (usuario, embudo de 401, logout) vive en un `SessionProvider` en el layout raíz; el guard es client-side (un `middleware.ts` queda como mejora futura). Tras el login se aterriza en la primera sección visible según permisos (el alcalde, directamente en el asistente).
- Cada ruta monta solo su controlador y carga sus datos al entrar; desaparecen las ~11 peticiones del login y el N+1 de documentos fuera de `/proyectos`. Las cadenas de refresco entre dominios se eliminan: cada ruta recarga al montar.
- El controlador admin monolítico (2.218 líneas, 105 estados) se trocea en seis hooks por dominio (`app/lib/admin/`); las listas de otros dominios llegan por fetchers ligeros (`app/lib/fetchers.ts`).
- Selección, filtros y página viven en la URL (`?id=`, `?c=`, `?q=&page=`): deep-links, refresh y botón atrás funcionan. Los filtros de municipios, ordenanzas y requisitos se aplican en el servidor (cierra la búsqueda incompleta sobre la página de 50 y prepara el dataset INE).
- Sin dependencias nuevas: solo primitivas del App Router y CSS propio (sección "app shell" monocroma).


## ADR-015: Revocación de tokens y rate limiting por cuenta (2026-06-12)

La revisión de los commits del día (refactor multi-ruta y sesión por cookie) dejó flecos de autenticación que se cierran en bloque:

- Los JWT llevan `iat` y los usuarios `password_changed_at`: un token emitido antes del último cambio o reset de contraseña queda revocado — el caso que motiva un reset ("cuenta comprometida") ya no deja viva la sesión del atacante. El cambio self-service reemite la cookie para no cerrar la sesión propia (también el reset desde admin cuando el administrador se lo aplica a sí mismo). El logout sigue sin revocar el JWT (stateless asumido; se revisará cuando el rate limiting se mueva a Redis).
- El rate limit del login se indexa por cliente+cuenta y solo los intentos fallidos consumen cupo: el hueco se reserva atómicamente antes de la verificación (lenta) de la contraseña y se reembolsa en éxito, de modo que las peticiones concurrentes tampoco pueden superar el tope. Con docker-proxy (o un reverse proxy futuro) todos los navegadores comparten IP: la clave solo-IP era en la práctica un cupo global de 10 logins/min que podía bloquear el acceso de toda la organización, y contar los logins correctos lo agravaba. Contrapartida aceptada: el password spraying entre cuentas desde una misma IP ya no comparte cupo; se valorará un límite secundario por cuenta al migrar a Redis.
- `POST /auth/change-password` recibe el mismo rate limit (indexado por usuario): la verificación de la contraseña actual es la defensa frente a una sesión secuestrada y era forzable sin tope.
- `POST /auth/logout` exige sesión: un formulario cross-site podía cerrar la sesión de la víctima (el Set-Cookie de borrado se aplica en contexto first-party).
- Las contraseñas no se recortan nunca: `AdminUserUpdate` aplicaba `str_strip_whitespace` también al reset del admin y almacenaba una credencial distinta de la tecleada.

## ADR-016: Hermes Agent como runtime privado con memoria controlada (2026-06-15)

Hermes Agent se trata como una aplicación/runtime de agente, no como modelo propio ni como almacén oficial de memoria institucional. El backend puede usarlo mediante su API Server privado compatible con OpenAI (`/v1/chat/completions`) configurando `ASSISTANT_RUNTIME=hermes_agent`, `HERMES_AGENT_BASE_URL`, `HERMES_AGENT_API_KEY` y `HERMES_AGENT_MODEL`.

No se delegan en Hermes Agent las decisiones de seguridad del producto: RBAC multi-tenant, auditoría por acción, revisión humana de memoria y trazabilidad siguen residiendo en esta aplicación. El único punto de salida sigue siendo `app/assistant/gateway.py`; ahí se adapta el contrato interno del asistente al formato OpenAI-compatible de Hermes Agent y se registran solo metadatos (`runtime`, modelo, tokens, motivo de parada), nunca contenido conversacional.

La memoria institucional queda controlada por el backend propio. El agente puede invocar `propose_memory_entry`, pero esa llamada solo crea una entrada `proposed`. Una persona con `assistant.memory.review` debe aprobar, editar, rechazar, archivar o bloquear la entrada. Solo las entradas `approved`, dentro de organizaciones donde el usuario tenga `assistant.memory.view`, se reutilizan en turnos posteriores.

En producción, el runtime Hermes Agent queda bloqueado para datos reales salvo activación explícita con `HERMES_AGENT_REAL_DATA_ALLOWED=true`. El despliegue recomendado es que Hermes Agent escuche en red privada o loopback, con `API_SERVER_KEY` configurado en el servicio Hermes y el mismo valor en `HERMES_AGENT_API_KEY` del backend.

## ADR-017: Intenciones conversacionales antes de agentes internos (2026-06-25)

> Parcialmente superado por ADR-020: las preguntas de capacidades ya no se responden con handlers deterministas; el modelo las redacta desde un prompt que fija el contrato de producto. Sigue vigente que una pregunta de capacidad no debe ejecutar acciones por sí sola.

Los agentes internos del asistente (`requirements_intake` y `consultation`) representan techos de herramientas y riesgo, no capacidades visibles de producto. Para evitar que preguntas generales caigan accidentalmente en un agente parcial, el backend clasifica primero ciertas intenciones directas con `TurnIntent`: `global_capabilities`, `read_requirements`, `create_requirement`, `create_test_requirement` y `unknown`.

Las preguntas globales de capacidades (“¿qué puedes hacer?”, “¿puedes crear requisitos?”) se contestan con un contrato estable de producto antes del planner y sin llamar al runtime de IA: el asistente puede consultar información visible, estructurar trabajo y crear/actualizar necesidades como borrador cuando los permisos lo permitan, pero no aprueba trámites ni valida decisiones oficiales. Una pregunta de capacidad no inicia por sí sola un flujo operativo de creación; para eso debe haber intención de trabajo (“crea…”, “apunta…”, “registra…”) y el backend seguirá pidiendo organización/contenido mínimo y comprobando duplicados.

Los mensajes persistidos por handlers deterministas guardan `routing.reason` y, cuando existe una intención explícita, `routing.intent`. El planner Hermes, si está activado, queda reservado para turnos que todavía necesitan seleccionar un agente operativo; no es autoridad de permisos, auditoría ni memoria.

## ADR-018: Importación jurídica supervisada y canal Telegram (2026-06-17)

La comparación de ordenanzas arranca como flujo supervisado, no como automatismo jurídico: los trabajos de importación (`ordinance_import_jobs`) se ejecutan con Redis/RQ, buscan y procesan solo fuentes oficiales configuradas, crean ordenanzas en `pending_review`, dividen el texto en fragmentos citables y guardan una revisión automática con checklist y score. La aprobación final exige `ordinances.review`; el agente nunca marca una ordenanza como vigente/aprobada por sí solo. Para el primer MVP se acota la carga a pueblos de la provincia de Burgos y se siembra el BOP de Burgos como fuente oficial primaria, dejando Castilla y León completa para una fase posterior.

Sprint 1 sustituye la dependencia de búsqueda web general para Burgos por un conector determinista BOPBUR (`backend/app/ordinances/bop_burgos.py`): usa el formulario oficial de `/busqueda`, parsea metadatos y PDFs oficiales, y alimenta los jobs cuando la fuente activa es `bopbur.diputaciondeburgos.es`. La cobertura operativa se expone como reporte medible en `GET /ordinances/coverage/burgos`, incluyendo fallos de importación/OCR que requieren revisión manual; el chunking intenta respetar artículos/disposiciones antes de caer a fragmentos por tamaño. El reporte se agrupa por nombre de municipio para que altas duplicadas locales no inflen la cobertura lógica. Los embeddings fallidos de Burgos se reintentan de forma acotada desde `POST /ordinances/coverage/burgos/retry-embeddings`.

La base jurídica semántica usa PostgreSQL como almacén único. En este Sprint 0 los embeddings se guardan como JSON text porque el modelo SQLAlchemy y la similitud local determinista (`EMBEDDINGS_RUNTIME=local_hash`) leen/escriben vectores como cadenas y puntúan en Python; una migración futura a `pgvector` debe introducir primero un tipo SQLAlchemy compatible y similitud en base de datos. En producción los embeddings pueden apuntar a un proveedor OpenAI-compatible configurado por variables de entorno. El agente de consulta puede usar una herramienta interna read-only para buscar chunks aprobados de ordenanzas (`semantic_search_ordinances`) con permiso `ordinances.compare`; la herramienta acepta filtros estructurados por municipio (`municipality_id` o `municipality_name`) y materia (`topic`) para acotar el corpus antes de puntuar embeddings. Si no hay resultados aprobados, debe reconocer la falta de cobertura en lugar de inventar normativa.

Telegram se incorpora como canal del asistente, no como identidad nueva: un usuario autenticado genera un código de un solo uso con caducidad corta, lo envía al bot y el chat queda vinculado a su usuario. Las conversaciones Telegram se guardan con canal separado y todas las acciones siguen usando el RBAC del usuario vinculado.

## ADR-019: Mapa municipal como capa geográfica compartida (2026-06-28)

El mapa municipal se implementa como dominio `geo` reutilizable, no como columnas geográficas incrustadas en necesidades o proyectos. `geo_locations` guarda la ubicación y `entity_locations` vincula esa ubicación con entidades de negocio; en v1 las entidades admitidas son `requirement` y `project`.

La primera versión usa puntos con `latitude`, `longitude` y `geometry_json`. El GeoJSON sigue RFC 7946, por lo que las coordenadas se guardan en orden `[longitude, latitude]`. Se aplaza PostGIS porque la imagen actual de desarrollo usa PostgreSQL/pgvector y porque el primer caso de uso solo necesita marcadores puntuales; la lógica de geometría queda aislada para migrar a tipos espaciales más adelante.

La seguridad del mapa combina permisos específicos (`map.view`, `map.edit`, `map.import`, `map.manage`) con las reglas normales de visibilidad de cada entidad. Tener acceso al mapa no basta para ver una necesidad/proyecto inaccesible, y tener acceso a una entidad no basta si falta permiso de mapa en su organización.

En frontend se usa Leaflet directo en un componente cliente, con import dinámico y marcadores propios, evitando React-Leaflet en esta primera versión para reducir riesgo de SSR/compatibilidad con Next.js y React.

## ADR-020: Anacleto v2 delega la conversación al modelo (2026-07-06)

El asistente deja de responder mediante handlers deterministas, planner semántico y router multi-agente. Cada turno web o Telegram entra en un único motor model-first (`app/assistant/turn.py`) con agente persistido como `anacleto`, `routing=null`, catálogo de herramientas filtrado por permisos y guardas de seguridad en código. Las respuestas fijas quedan limitadas a error de gateway, respuesta vacía, refusal del proveedor y errores HTTP.

La supervisión humana no depende del prompt: la primera llamada a `create_requirement` prepara la propuesta, pero queda bloqueada y el servidor añade al mensaje una representación canónica de todos los valores que se persistirían. El backend guarda la propuesta normalizada y un digest SHA-256 en `assistant_conversations.state`; solo la ejecuta si el usuario responde de forma explícita a ese mensaje y el modelo repite en el mismo turno el payload efectivo completo. La autorización se consume una sola vez bajo bloqueo de fila, no se recupera si el ejecutor falla y cualquier cambio genera una propuesta y confirmación nuevas. Preguntas y debate no autorizan la escritura, y una cancelación explícita elimina el pendiente. Los ejecutores siguen aplicando RBAC/tenancy y dejan rastro en `assistant_messages.actions`.

La web usa `POST /assistant/conversations/{id}/messages/stream` con SSE para `message_start`, `text_delta`, `tool_activity` y `done`; el POST clásico se conserva para compatibilidad y Telegram. El frontend lee el stream con `fetch`, muestra deltas en vivo, renderiza Markdown seguro con `react-markdown` + `remark-gfm` y reemplaza el contenido provisional por el mensaje canónico del `done`.

Se retiran las variables `ASSISTANT_PLANNER_*` y se añade `ASSISTANT_HISTORY_MAX_MESSAGES` para acotar la ventana enviada al modelo. El coste asumido es al menos una llamada LLM por turno; se mitiga con streaming, historial limitado, prompt estable y eliminación de llamadas adicionales de planner/router. Todo egreso IA sigue pasando solo por `gateway.py`, con logs de metadatos y sin documentos originales.

## ADR-021: Pipeline de voz en nube gestionada con doble punto de egreso (2026-07-07)

El diálogo por voz pasa a ser requisito de producto (petición del alcalde) y se implementa 100% sobre nube gestionada: no hay infraestructura propia donde autoalojar Whisper/Piper, así que se supersede ADR-012 (reconocimiento solo local en el navegador). La entrada de voz ya egresaba audio a NVIDIA NIM (`app/assistant/speech.py`, runtime `nvidia_nim`) sin ADR que lo cubriera; esta decisión lo regulariza: `speech.py` queda reconocido como el segundo y último punto de egreso de IA junto a `gateway.py`, con su misma disciplina — por él solo viajan audio del turno y texto de respuesta del asistente, nunca documentos originales, y los logs registran únicamente metadatos.

La síntesis de voz usa Azure AI Speech (REST, voz `es-ES-ElviraNeural`, runtime conmutable `SPEECH_SYNTHESIS_RUNTIME=disabled|azure`) sobre una suscripción Azure for Students, válida solo para desarrollo y evaluación: sus términos excluyen cargas comerciales/de producción, caduca con la condición de estudiante y suspende recursos al agotar el crédito. Antes de operar con datos reales, el recurso Speech debe recrearse en una suscripción pay-as-you-go propia (cambio limitado a variables de entorno) y revisarse la posición DPA/ENS del proveedor; el STT `nvidia_nim` queda como proveedor en evaluación con la misma condición y candidatos de sustitución ya investigados (docs/investigacion-api-ia.md §6.2).

El estilo oral se decide por turno con `input_mode` (web y Telegram), altera el system prompt y hace que los bloques canónicos generados por el servidor usen texto oral sin Markdown; no se persisten columnas nuevas. Esos bloques también se emiten como `text_delta` para que el modo manos libres lea la propuesta exacta y la petición de confirmación. El navegador no usa reconocimiento ni síntesis propios (se retira la vía `processLocally` de ADR-012 y no hay fallback a `speechSynthesis`): captura y reproduce, y toda la voz pasa por los endpoints del backend, manteniendo RBAC, tenancy y auditoría existentes. Especificación completa: docs/diseno-dialogo-voz.md.

## ADR-022: Cascarón del módulo Ayuntamiento con contenido por bloques (2026-07-29)

La pantalla «Ayuntamiento» del diseño (Claude Design, `Pantalla Principal.dc.html`) deja al usuario crear, renombrar, reordenar y borrar libremente los apartados de su municipio. Se implementa primero el cascarón —barra superior del municipio, menú configurable y su editor— sobre un modelo de **bloques genéricos** (`municipal_blocks`: padre, posición, título, cuerpo y carga libre en JSON) en lugar de tablas tipadas por dominio. Tablas tipadas serían más consultables y validables, pero obligarían a una migración por cada epígrafe nuevo y no admiten el «añade el apartado que quieras» que el diseño da por supuesto. El check de `block_type` ya admite los tipos reservados de contenido (`epigraph`, `section`, `item`) para que la fase siguiente no necesite migración; la API solo crea los dos de navegación.

El contenido cuelga de `Organization`, no de `Municipality`. `Municipality` es dato de referencia **global compartido entre inquilinos** (ADR-003), mientras que la ficha del Ayuntamiento la edita cada inquilino para sí: colgarla del municipio filtraría contenido editable entre organizaciones. El perfil (`municipal_profiles`) guarda nombre mostrado, escudo y el interruptor del bloque de temperatura, uno por organización.

El escudo **no** usa el modelo `Document`: `documents.project_id` es `NOT NULL` y su control de acceso es el del proyecto, ninguna de las dos cosas encaja con una imagen de marca de la organización. Reutiliza el servicio de almacenamiento bajo el prefijo `organizations/<id>/brand/`, conservando escritura por trozos, checksum y defensa de path traversal, con lista blanca propia (solo imágenes) y tope propio (2 MiB).

Se adopta además el cromo del diseño: desaparece la barra superior global (tema, atajo al asistente y avatar) y el conmutador de tema pasa a ser una fila del menú lateral, que gana plegado. La ayuda inicial deja de señalar botones de posición fija y mide la fila resaltada del menú.

El bloque de temperatura usa **Open-Meteo** consultado **desde el backend** (`app/town_hall/weather.py`), nunca desde el navegador: así el ayuntamiento no expone a sus usuarios a un tercero. Es un punto de egreso externo nuevo, el primero que no es de IA, y por eso queda fuera del gateway de ADR-013/ADR-021 pero se somete a su misma disciplina: por él solo salen el nombre público de la localidad —una sola vez, para geocodificarla— y sus coordenadas, jamás datos de usuarios, documentos ni conversaciones, y los logs registran solo metadatos. Open-Meteo se elige sobre AEMET por no requerir clave de API ni alta; el dato es orientativo, no oficial. Las coordenadas resueltas se guardan en `municipal_profiles` y se invalidan al cambiar la localidad. La temperatura se cachea en memoria 30 minutos, con la misma condición que el limitador de login: mover a Redis antes de ir a multi-worker (ADR-010). Se pide en un endpoint aparte de la pantalla, de modo que una caída del proveedor no impida cargar el Ayuntamiento: el bloque muestra entonces un guion, nunca una cifra inventada.
