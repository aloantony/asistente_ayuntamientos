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

La compatibilidad OpenAI del API Server no demuestra que Hermes haya desactivado sus toolsets nativos. Esas herramientas podrían operar fuera de RBAC, auditoría y la frontera post-taint del backend. Por ello Hermes falla cerrado salvo atestación técnica explícita mediante `HERMES_AGENT_NATIVE_TOOLS_DISABLED_CONFIRMED=true`; el indicador solo se activa después de verificar la configuración y el comportamiento efectivo del servidor principal, no por una instrucción de prompt. Incluso con esa atestación, el catálogo y `execute_tool` omiten/rechazan `web_search` y `read_web_page` cuando `ASSISTANT_RUNTIME=hermes_agent`, de modo que ningún snippet o cuerpo externo entra en una ronda conversacional posterior de Hermes. El proveedor Hermes de búsqueda, si se selecciona expresamente, sigue siendo una instancia separada y controlada; Anthropic, OpenAI Responses y el puente Codex mantienen las herramientas web del backend.

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

## ADR-022: Brave como proveedor controlado de búsqueda web (2026-07-15)

La búsqueda web pública se desacopla de Hermes mediante una única fachada backend (`app/assistant/web_search.py`) compartida por Anacleto y el descubrimiento de fuentes de ordenanzas. `WEB_SEARCH_PROVIDER=brave|hermes|disabled` selecciona un único proveedor de forma explícita; una configuración incompleta, un límite de cuota o un fallo de Brave nunca provoca fallback a Hermes, porque eso enviaría la consulta a otro encargado sin una decisión consciente. La plantilla de nuevas instalaciones selecciona Brave; el valor interno sigue temporalmente en Hermes para que una actualización no cambie de encargado sin modificar el entorno.

El adaptador Brave usa exclusivamente el endpoint HTTPS fijo de Web Search y mantiene la clave en servidor. Envía la consulta pública normalizada —máximo 400 caracteres y 50 palabras— sin historial, documentos ni cabeceras de localización; fija versión de API, filtrado estricto de contenido adulto, límite de cinco resultados para la herramienta, timeout de 15 segundos, respuesta máxima de 1 MiB y rechazo de redirecciones. RBAC, auditoría, DLP y saneado de URLs siguen en el backend. Los snippets se consideran entrada externa no confiable y el modelo tiene prohibido obedecer instrucciones contenidas en ellos.

El rastro de acciones conserva la consulta, URLs y snippets en todos los entornos. Brave exige derechos contractuales específicos para almacenar resultados total o parcialmente y documenta retención de consultas; por eso el cliente queda deshabilitado mientras `BRAVE_SEARCH_STORAGE_RIGHTS_CONFIRMED=false`, también en desarrollo. Ese indicador solo puede activarse después de confirmar un plan compatible, DPA/SCC, retención y política para nombres/direcciones. Referencias: [privacidad del API](https://api-dashboard.search.brave.com/privacy-policy), [términos del API](https://api-dashboard.search.brave.com/documentation/resources/terms-of-service) y [contrato Web Search](https://api-dashboard.search.brave.com/api-reference/web/search/get).

## ADR-023: OpenAI Responses como runtime stateless de Anacleto (2026-07-15)

Anacleto incorpora `ASSISTANT_RUNTIME=openai_responses` como motor conversacional adicional. No se sustituye la aplicación por la web de ChatGPT: conversación, memoria, RBAC, tenancy, herramientas, confirmaciones de escritura y auditoría siguen siendo responsabilidad del backend. La integración requiere un proyecto y una clave de la API de OpenAI con facturación propia; una suscripción de ChatGPT no concede acceso a la API.

El runtime usa la Responses API con `store=false`, `include=["reasoning.encrypted_content"]` y `gpt-5.6` como modelo configurable inicial. `OPENAI_RESPONSES_MAX_OUTPUT_TOKENS` parte de 25.000 porque el límite engloba razonamiento y salida visible; se mantiene separado del límite de Anthropic. Los elementos completos de `response.output`, incluido el razonamiento cifrado y el `phase` de cada mensaje, se conservan solo en memoria durante el bucle de herramientas y se reenvían junto al `function_call_output` identificado por `call_id`; no se guardan en PostgreSQL. Las respuestas históricas persistidas se reenvían como `phase="final_answer"`. Las herramientas se publican como funciones planas con `strict=false` para mantener compatibilidad con el catálogo JSON Schema existente. El backend sigue validando argumentos, límites, permisos y ejecución. Una migración futura a esquemas estrictos exige adaptar y probar todo el catálogo antes de activar `strict=true`.

El egreso se limita a hosts HTTPS oficiales de OpenAI y rechaza redirecciones. El streaming interpreta eventos tipados de Responses, exige un terminal válido y aplica límites de tiempo y tamaño; no hay reintentos ocultos. Cada solicitud incluye un `safety_identifier` HMAC estable, compartido con Realtime, que permite correlación de seguridad sin revelar el identificador interno del usuario. Los logs locales continúan siendo solo metadatos.

La herramienta alojada `web_search` de OpenAI no se expone en la conversación principal. La búsqueda pública sigue siendo una función propia del backend para validar preventivamente RBAC y DLP, auditar la consulta y normalizar las fuentes antes de devolverlas al modelo. El proveedor de búsqueda se integra y selecciona por separado, sin fallback automático entre proveedores. Así puede incorporarse Brave sin acoplar el gobierno de búsqueda al runtime conversacional.

Los deltas SSE son provisionales hasta el terminal. Si una continuación, un preámbulo de herramienta o un fallo hace que su concatenación no coincida con el mensaje persistido, el backend emite `text_reset` con el contenido canónico antes de `done`; el frontend reemplaza el borrador. La voz por turnos no sintetiza deltas provisionales y espera a `done`, evitando leer comentarios o fragmentos que después se descarten.

`store=false` evita persistir el estado de Responses, pero no equivale por sí solo a retención cero: antes de producción con datos reales deben cerrarse contrato, región y controles de retención (MAM/ZDR cuando proceda). Un proyecto aprobado para residencia regional puede configurar `https://eu.api.openai.com/v1`; el endpoint global queda como valor de desarrollo por defecto. Referencias: [Responses API](https://developers.openai.com/api/reference/resources/responses), [function calling](https://developers.openai.com/api/docs/guides/function-calling), [controles de datos](https://developers.openai.com/api/docs/guides/your-data) y [residencia regional](https://developers.openai.com/api/docs/guides/your-data#regional-processing-and-data-residency).

## ADR-024: Fundación del hub municipal sobre los dominios existentes (2026-07-15)

El export de diseño añadido en el commit `4405051` es una referencia de producto para descubrir flujos y prioridades, no código de aplicación ni contrato de datos. Su HTML, CSS, estado de navegador y contenido de ejemplo no se incorporan al producto ni se consideran fuentes autorizadas; en particular, no se importan los datos personales, políticos, normativos o municipales incluidos en el prototipo.

El hub municipal se construye componiendo los dominios existentes: `Organization` sigue siendo el tenant y contexto de trabajo, `Municipality` el dato municipal de referencia, `Ordinance` la fuente normativa canónica, `Document` el canal controlado para archivos y `GeoLocation` la base geográfica. La primera entrega es una vista de solo lectura, sin migración ni importación, que consume únicamente datos reales disponibles en esos contratos. Los campos ausentes se muestran como estado vacío o pendiente de fuente; nunca se rellenan con datos ficticios.

El contexto del hub no presupone un único ayuntamiento por usuario. La selección explícita de organización y municipio deriva de las organizaciones visibles devueltas por la sesión y conserva ambos identificadores. En esta primera lectura, `Municipality` continúa siendo un catálogo de referencia protegido por `municipalities.view|manage`; ese acceso no autoriza datos privados de la organización. Cada módulo tenant posterior debe recibir la organización seleccionada y validar en el backend la pertenencia, el vínculo municipal y sus permisos específicos. Ocultar controles o filtrar en el frontend mejora la experiencia, pero la autorización y el aislamiento multi-tenant del servidor son siempre la fuente de verdad.

Las capacidades sugeridas por el prototipo se mantienen como dominios separados, no como una tabla o pantalla municipal monolítica: inventario/GIS amplía la capa `geo`; mantenimiento gestiona órdenes y ciclos de vida de activos; el CMS gestiona contenido municipal con procedencia y publicación; RR. HH. queda aislado por contener datos personales y laborales de especial sensibilidad. La biblioteca normativa reutiliza `Ordinance`, y cualquier ampliación del alcance actual de `Document` se diseña y migra en su propio cambio.

El trabajo se entrega mediante PRs apilados y reversibles: (1) shell y contexto municipal de solo lectura; (2) contrato, migración y API del inventario/GIS; (3) visualización y edición de activos en el mapa; (4) mantenimiento y auditoría; (5) CMS y biblioteca normativa; y (6) descubrimiento jurídico y de privacidad de RR. HH. antes de autorizar su implementación. Cada PR depende solo del anterior cuando comparte contrato y conserva pruebas de permisos y aislamiento.

Ningún flujo de escritura o importación avanza sin un gate explícito que documente clasificación y minimización de datos, fuente y procedencia, matriz RBAC/tenancy, auditoría, retención/archivo y validación segura de archivos y formatos. Las importaciones normativas mantienen revisión humana conforme a ADR-018; los adjuntos pasan por el almacenamiento backend de `Document`, nunca por `localStorage` o IndexedDB. RR. HH. exige además análisis jurídico y de impacto de protección de datos antes de diseñar su esquema. Toda migración posterior debe validar tanto una base nueva como la actualización desde la última revisión desplegada.

## ADR-025: Inventario municipal tenant-scoped sobre la capa geográfica (2026-07-15)

El inventario se incorpora como dominio backend propio, compuesto por categorías, tipos y activos municipales. `Organization` continúa siendo el tenant y determina de forma canónica el `Municipality` de cada activo; el cliente no puede elegir un municipio distinto. Un activo puede apuntar a una `GeoLocation` existente, pero solo cuando organización y municipio coinciden exactamente. Las claves foráneas compuestas protegen en PostgreSQL los vínculos categoría→tipo, tipo→activo, organización→municipio y ubicación→tenant, además de las validaciones de la API. Una organización con activos no puede reasignar su municipio y una ubicación enlazada no puede cambiar de tenant; los metadatos y coordenadas de esa ubicación sí siguen siendo editables.

El ciclo de vida evita borrado físico: categorías y tipos usan `active|archived`, y los activos `active|inactive|retired|archived`. Las taxonomías archivadas siguen siendo legibles para conservar contexto histórico, pero no admiten nuevos descendientes. Una organización activa permite escribir, una pausada solo leer y una archivada no expone el inventario. Las escrituras requieren también que el municipio vinculado esté activo.

La autorización se separa en `assets.view|create|edit|archive|manage` y se evalúa siempre dentro de la organización objetivo. Archivar no concede edición: una operación mixta exige ambas capacidades, y crear un registro ya archivado exige crear y archivar. `assets.view` incluye la ubicación completa de la ficha del activo, pero no concede acceso al listado geográfico transversal ni edición: esos flujos conservan `map.view|edit`. Los listados se paginan, excluyen archivados por defecto y precargan tipo, categoría y ubicación para mantener un número acotado de consultas.

Esta entrega no amplía todavía `geo/map-items`, no crea geometrías, no incorpora frontend, adjuntos, importaciones ni mantenimiento. El contrato operativo queda documentado en `docs/inventario-municipal.md`; la siguiente entrega apilada puede representar y editar activos sobre el mapa reutilizando esas entidades.

## ADR-026: Activos municipales en el mapa mediante ubicación copy-on-write (2026-07-15)

Los activos pasan a formar parte de `GET /geo/map-items`, pero no de `entity_locations`: `municipal_assets.location_id` continúa siendo su única relación geográfica y la fuente de verdad. Esto evita dos vínculos susceptibles de divergir. El listado combina autorización de ambos dominios dentro de la misma organización: requiere `map.view|manage` y `assets.view|manage`; además oculta ubicaciones rechazadas, organizaciones archivadas y, salvo petición explícita, activos archivados.

`POST /geo/entity-locations` acepta `entity_type=asset` únicamente para la ubicación `primary`. Reubicar un activo exige edición en los dos dominios (`map.edit|manage` y `assets.edit|manage`) además de poder leer el inventario. El servidor bloquea el activo durante la operación, deriva organización y municipio de sus datos canónicos y rechaza cualquier ámbito distinto enviado por el cliente. Una organización pausada o archivada, o un municipio inactivo, no admite la escritura.

Cada reubicación cartográfica mediante el dominio `geo` usa copy-on-write: crea una `GeoLocation` nueva y reasigna solo `municipal_assets.location_id`, sin modificar ni eliminar la ubicación anterior. Las actualizaciones de necesidades y proyectos adoptan la misma regla al reasignar su `EntityLocation`. Así una ubicación compartida nunca desplaza otra entidad como efecto colateral. La fila del activo y los vínculos ya existentes se bloquean durante la reasignación; dos altas iniciales concurrentes de un mismo vínculo no pueden bloquear un hueco inexistente, por lo que la restricción única conserva una sola y la otra devuelve un conflicto reintentable. Las ubicaciones de activos creadas por este flujo fuerzan `source=user_provided` y `review_status=proposed`; el cliente no puede autoaprobar su propia edición. La limpieza posterior de ubicaciones huérfanas se reserva a una tarea explícita y auditable.

Aunque ADR-025 permite la relación en el modelo y en procesos internos controlados, `location_id` queda de solo lectura en los esquemas HTTP de inventario. Aceptar un id arbitrario en `POST|PATCH /assets` permitiría a alguien con permisos de inventario convertir en visible la ubicación de una necesidad o proyecto del mismo tenant a la que no tuviera acceso. El dominio `geo` es por ahora el único escritor público de esa relación. Una futura reutilización de ubicaciones revisadas o importadas deberá definir un pool explícitamente visible, procedencia y auditoría; conocer el identificador no concede acceso.

El listado aplica visibilidad, estado y tenancy en SQL antes de combinar candidatos; ordena los tres dominios por actualización de ubicación y aplica un único límite global con desempate total. Después hidrata las entidades en bloques, sin consultas por marcador. Un filtro por `entity_id` requiere también `entity_type` para evitar colisiones entre identificadores de dominios distintos. Cada resultado incluye `role`, y la identidad cartográfica completa es `(entity_type, entity_id, role)`; los activos usan siempre `primary`.

La interfaz de `/mapa` añade filtro y marcadores de activos, y permite elegir un activo existente desde el menú contextual para ubicarlo o reubicarlo. No crea activos ni sustituye una futura pantalla de inventario. Esta entrega no necesita migración porque reutiliza las claves y restricciones tenant-scoped de ADR-025.

## ADR-027: Mantenimiento de activos con auditoría append-only (2026-07-15)

El mantenimiento se implementa como dominio propio sobre `municipal_assets`, no como campos mutables de la ficha ni como tareas de `agent_office`. Las órdenes representan trabajo humano y tienen estados `planned|scheduled|in_progress|completed|cancelled`; los eventos representan el rastro institucional inmutable. Esta separación permite que una automatización futura proponga o cree trabajo mediante el servicio autorizado sin convertir una orden municipal en una ejecución interna del asistente.

La organización y el municipio se derivan del activo. Una clave foránea compuesta mantiene orden y activo en el mismo tenant, y la asignación opcional solo acepta usuarios activos pertenecientes a esa organización. Los permisos `maintenance.view|create|edit|complete|manage` se evalúan por organización y se combinan con `assets.view|manage`, porque acceso al mantenimiento no debe revelar por otra ruta un activo oculto. Una organización activa admite escrituras, una pausada conserva lectura y una archivada no expone el dominio.

El estado no se modifica mediante el `PATCH` genérico: un endpoint de transición bloquea primero el activo y después la orden, valida el grafo `planned→scheduled|in_progress`, `scheduled→planned|in_progress` e `in_progress→completed`, y añade el evento en la misma transacción. Completar exige `maintenance.complete|manage` y no se permite el salto directo desde `scheduled`; programar, desprogramar e iniciar exigen `maintenance.edit|manage`. Cancelar cualquier orden abierta o reabrir una terminal exclusivamente hacia `planned` requiere `maintenance.manage` y un motivo no vacío. Creación y edición también generan eventos server-side; no existe API para editar o borrar órdenes o eventos. Las claves usan `RESTRICT` y una guarda PostgreSQL impide alterar o borrar el historial. Los eventos evitan payloads arbitrarios y registran solo actor, momento, transición, una lista blanca de campos afectados y una nota acotada cuando procede.

Crear rechaza activos retirados o archivados. Retirar o archivar un activo bloquea su fila y falla mientras existan órdenes abiertas; las escrituras de mantenimiento siguen el mismo orden de locks para evitar la carrera orden-activo. Una asignación solo admite usuarios activos miembros del tenant y se revalida antes de iniciar o completar. Los identificadores invisibles o de otro tenant producen un 404 genérico, mientras los listados aplican ambos dominios de permisos en SQL antes de devolver resultados.

Los índices siguen las consultas operativas por organización/estado/fecha, activo/estado/fecha y responsable/estado/fecha. La interfaz se compone dentro del detalle de activo de `/mapa`, con próximos trabajos, vencimientos, historial y acciones autorizadas. Se aplazan recurrencias, notificaciones, costes, partes de horas, adjuntos, configuración libre y ejecución autónoma; esos flujos requieren decisiones propias de retención, privacidad y operación. El contrato completo se documenta en `docs/mantenimiento-municipal.md`.
## ADR-028: Suscripción Codex como puente local desechable (2026-07-16)

Anacleto admite `ASSISTANT_RUNTIME=codex_subscription` únicamente cuando `ENVIRONMENT=development`, como vía transitoria para evaluar el producto antes de contratar una API de producción. La integración usa la interfaz oficial `codex app-server` y su autenticación ChatGPT gestionada; no convierte la suscripción en una clave API, no llama a endpoints privados copiados de la web y no sustituye el backend propio. La configuración queda rechazada por validación fuera de desarrollo, incluso si existen binario y credenciales, y exige los opt-in independientes `CODEX_SUBSCRIPTION_ENABLED=true` y `CODEX_SUBSCRIPTION_REAL_DATA_ALLOWED=true`, ambos `false` por defecto.

Las herramientas municipales se publican como `dynamicTools`, pero no se ejecutan dentro del gateway. Cuando app-server envía `item/tool/call`, el gateway conserva el proceso y la request pendientes, devuelve al bucle existente una llamada con nombre/argumentos normalizados y guarda en `provider_state` solo un handle aleatorio en memoria. `turn.py` mantiene RBAC, tenancy, confirmaciones, presupuestos, detección de repeticiones, auditoría y Brave; después devuelve el resultado al mismo request y reanuda el turno. Las sesiones son efímeras, tienen propietario, exclusión de uso, TTL, límite global y limpieza en terminal, error, timeout o cancelación. La síntesis forzada destruye primero una sesión con herramientas porque app-server no permite retirar `dynamicTools` de un turno activo.

El protocolo de herramientas dinámicas es experimental y se ha validado contra `codex-cli 0.144.4`. El proceso se lanza sin shell, con allowlist de entorno, cwd temporal vacío, `CODEX_HOME` dedicado con modo `0700`, `auth.json` regular, no symlink y `0600`, autenticación obligatoria de tipo ChatGPT, historia desactivada, sandbox de solo lectura y aprobaciones `never`; se desactivan las capacidades internas conocidas y se aborta si aparece actividad de shell, archivos, web, MCP, apps, navegador, imágenes o subagentes. No se reutiliza `~/.codex`, porque podría cargar configuración, plugins, skills, MCP e historial personales. Estas defensas reducen superficie, pero no existe un kill switch documentado que convierta Codex en un modelo neutro sin herramientas internas: el proceso debe vivir además en un entorno aislado y sin secretos del backend. La evaluación ejecuta hoy el backend en ese host con PostgreSQL/Redis en Docker; un sidecar por socket sería una evolución y no está implementado.

Se acepta que esta vía no tiene SLA, comparte identidad y cuotas ChatGPT, puede sufrir capacidad o límites, añade procesos/OAuth/latencia y no reproduce exactamente Responses. Antes de producción se cambia a `openai_responses` (u otro proveedor aprobado), se repite la evaluación funcional y se cierran contrato, residencia, retención, observabilidad y costes. Guía operativa: `docs/runtime-codex-suscripcion.md`. Referencias: [Codex app-server](https://learn.chatgpt.com/docs/app-server), [autenticación](https://learn.chatgpt.com/docs/auth#openai-authentication) y [configuración](https://developers.openai.com/codex/config-reference).

## ADR-029: Biblioteca jurídica recuperable, revisada y segura por defecto (2026-07-16)

La consulta normativa pasa de una herramienta interna y una tabla administrativa a una biblioteca explícita en `/ordenanzas`. Su contrato paginado (`GET /ordinances/search`) puntúa el corpus completo, ofrece ámbitos por fragmento, ordenanza o municipio y devuelve junto a los resultados la política de estados jurídicos y la cobertura de datos poblacionales. Filtros, página, ficha y comparación viven en la URL. La similitud vectorial se considera solo afinidad de recuperación: no se muestra como confianza jurídica y nunca sustituye estado, fechas, fuente oficial o revisión humana.

El corpus recuperable exige conjuntamente ordenanza aprobada, chunk aprobado, embedding listo y modelo vigente. Se excluyen por defecto `repealed`, `superseded` y `archived`; `unknown` y `partially_repealed` siguen disponibles con advertencia obligatoria. La misma regla se aplica a búsqueda, comparación y resumen de cobertura de Anacleto. Cambiar texto reconstruye todos sus chunks como pendientes, y cambiar cualquier metadato jurídico sensible invalida la curación previa; aprobar es una decisión separada con `ordinances.review`. Las altas manuales ya no nacen aprobadas. Esta decisión amplía ADR-018 y sustituye su descripción temporal de una recuperación limitada y exclusivamente Python: PostgreSQL con la extensión `vector` hace hoy el ranking exacto y la paginación; la columna permanece textual y un índice ANN queda aplazado hasta fijar dimensión y medir completitud.

La reconstrucción valida todos los fragmentos antes de sustituir los existentes y
falla si excede el máximo configurado, en vez de truncar el texto silenciosamente.
Los embeddings de red se generan en RQ después del commit; la petición que crea
o edita la norma no mantiene una transacción ni espera al proveedor externo.

Las fuentes `official_only` dejan de confiar solo en un sufijo de hostname. Alta y edición validan URL base y dominio; cada descarga desactiva proxies y redirecciones automáticas, comprueba todas las IP resueltas y conecta al `sockaddr` validado. IP privadas/especiales, credenciales, puertos no estándar y saltos fuera de la fuente quedan rechazados. Las redirecciones se siguen manualmente con nueva validación y límite. Si un ítem declara fuente concreta, no puede aprovechar otro dominio permitido del mismo trabajo. Esta defensa cubre SSRF, DNS rebinding y pivotaje por redirección sin abrir una vía alternativa de red.

`Ordinance` sigue siendo global, pero `Document` sigue siendo tenant-scoped. Por ello los listados normativos no devuelven identificadores documentales y el detalle enmascara id y metadatos cuando el usuario no puede acceder al documento. La comparación devuelve todos los municipios solicitados aunque alguno no tenga filas, para que la interfaz diga «sin registro aprobado en este corpus» sin inferir ausencia de regulación. Permisos, estados, endpoints, límites y comprobación operativa se documentan en `docs/biblioteca-ordenanzas.md`.

## ADR-030: Adjuntos del chat autorizados para un único turno (2026-07-16)

El usuario puede seleccionar documentos ya accesibles de un proyecto o subirlos mediante el almacenamiento `Document` existente. La autorización se valida por documento y tenant antes de persistir el mensaje; un identificador ausente o ajeno devuelve el mismo 404 genérico. La relación durable conserva el documento, posición, estado/conteo de extracción y una instantánea de auditoría de actor, organización, proyecto, checksum, alcance y momento de autorización. El texto extraído nunca se guarda en mensajes ni en la tabla de relación: existe solo en memoria durante la petición que lo autorizó y no reaparece en el historial de turnos posteriores.

Solo TXT UTF-8 se lee localmente, con límites de bytes y caracteres, apertura sin seguimiento de symlinks y verificación de tamaño/checksum en una única lectura. El preflight devuelve una instantánea inmutable, sin referencias ORM necesarias para la extracción; antes de tocar el archivo se ejecuta `rollback()`, que libera la transacción de lectura y descarta también cualquier DML Core u ORM ajeno pendiente. Por tanto, la lectura ocurre sin transacción de base de datos activa ni locks de autorización o fila. Después se abre una transacción nueva: primero toma el advisory lock explícito del grafo RBAC y luego recarga/bloquea, en orden, conversación, usuario, documentos y proyectos hasta el commit corto. Todos los mutadores de membresías y asignaciones de organización, proyecto, grupo, rol y permiso usan el mismo punto canónico, sin `FOR UPDATE` implícito sobre joins. Ningún lock de autorización o fila cruza la lectura del archivo ni el egreso al proveedor.

PDF, DOCX, XLSX y cualquier otro formato estructurado se marcan `unsupported`: no se parsean en el proceso API hasta disponer de un servicio dedicado no-root, sin red, con filesystem de solo lectura y límites de cgroup. Las imágenes se conservan y previsualizan desde la descarga autenticada, pero se marcan `vision_unavailable` y no aportan contexto mientras no exista un proveedor de visión aprobado. Todo texto extraído se considera contenido no fiable, no instrucciones. En cualquier turno con adjuntos se elimina por código el catálogo completo de herramientas y `execute_tool` mantiene una segunda guarda fail-closed; nombre, identificador, argumentos y estado opaco de cualquier llamada alucinada se sustituyen antes del primer evento, firma o auditoría. Hermes Agent rechaza el turno antes de leer o enviar el adjunto porque su lista API vacía no permite demostrar que sus herramientas internas estén desactivadas.

## ADR-031: Lectura web con procedencia efímera y defensa SSRF (2026-07-16)

La búsqueda y la lectura se mantienen como herramientas separadas. `web_search` continúa usando un único proveedor seleccionado explícitamente conforme a ADR-022; este cambio no añade fallback automático. Solo las URLs incluidas en el resultado compacto que el modelo recibió se registran en memoria con consulta, proveedor y posición. `read_web_page` exige coincidencia exacta después de normalizar y no puede reutilizar esa procedencia en otro mensaje. El lector tiene un opt-in independiente (`ASSISTANT_WEB_READER_ENABLED=false` por defecto) y se excluye por completo de Realtime; la voz conserva solo los snippets acotados de `web_search`. `/assistant/status` expone por separado la disponibilidad del lector textual y su indisponibilidad en Realtime, que la interfaz también comunica.

El lector no es un navegador ni un proxy general. Ejecuta un GET anónimo sin cookies, autenticación, JavaScript ni compresión, limita tipos a HTML/XHTML o texto y deshabilita PDF remoto hasta disponer de extracción aislada. Solo permite egreso por los puertos 80/443 y acota bytes, texto, redirecciones y tiempo total. Rechaza credenciales en URL, nombres internos, endpoints de metadatos y cualquier IP que no sea pública. Validación, DNS, conexión/TLS, cabeceras, redirecciones, cuerpo y extracción se ejecutan en un único proceso desechable. El padre aplica un deadline absoluto desde antes de la admisión y lee el IPC por `socketpair(AF_UNIX, SOCK_STREAM)` no bloqueante y `selector`: recibe fragmentos bajo ese mismo deadline, exige EOF para completar el JSON y rechaza más de 512 KiB. Así un hijo detenido a mitad del mensaje se mata y recolecta sin depender de un framing bloqueante. DNS no crea otro subproceso: valida todas las respuestas antes de conectar y fija el socket a una IP validada manteniendo Host, SNI y verificación de certificado sobre el hostname.

Un semáforo global configurable mediante `WEB_PAGE_MAX_CONCURRENT_READERS` limita procesos y lanzadores; la admisión saturada falla rápido dentro del deadline. La creación de contexto, sockets y proceso normaliza fallos locales, y `Process.start()` se ejecuta en un launcher thread acotado por el deadline. Si el arranque retorna tarde, el launcher mata/recolecta el hijo, cierra los extremos y solo entonces libera el slot; el request ya ha retornado y no puede abrirse otro lector con ese slot. La liberación exige verificar que el proceso está muerto, recolectado y cerrado. Cualquier fallo o incertidumbre en kill, liveness, reap o close pone el lease permanentemente en cuarentena, nunca cierra un handle que pueda seguir vivo y emite el log crítico contador `web_reader_quarantined_slots_total`; se acepta degradar de forma acotada la capacidad antes que permitir doble propiedad sobre un worker. La secuencia acotada de reap/terminate/kill puede añadir como máximo 0,55 segundos de limpieza. Un primitivo de kernel totalmente bloqueado no puede cancelarse forzosamente desde Python y se acepta como fallo residual del host, pero el lease retenido mantiene acotada la concurrencia en lugar de acumular procesos. Cada redirección debe conservar exactamente esquema, hostname y puerto efectivo; si cambia el origen, el modelo debe iniciar otro turno de búsqueda para autorizar la nueva URL.

Títulos, snippets y texto extraído se consideran entrada externa no confiable. Tras el primer `web_search`, el runtime textual permite únicamente `read_web_page` sobre las URLs exactas de la procedencia inicial, incluidas varias fuentes de ese mismo resultado; su argumento debe ser exactamente el diccionario de una clave `{"url": "<URL canónica>"}`, con la cadena cruda idéntica a la clave canónica de procedencia. Fragmentos, credenciales, variantes de mayúsculas o normalización y campos adicionales se deniegan aunque apunten aparentemente al mismo recurso. Se bloquean también búsquedas posteriores, herramientas semánticas/locales, mutaciones y nombres desconocidos. Leer una página no amplía esa procedencia. Realtime no expone el lector y por ello bloquea toda herramienta tras la búsqueda. La frontera de `execute_tool` replica la política aunque se invoque fuera del bucle. Toda llamada denegada después del taint reemplaza nombre y argumentos completos por constantes antes de persistir o emitir estados, acciones y eventos; una lectura permitida emite y persiste solo una copia nueva del argumento canónico de una clave. Realtime sustituye además el `call_id` controlado por el modelo por un correlador HMAC-SHA-256 determinista, separado por dominio y ligado al secreto de la aplicación para impedir diccionarios offline. Realtime persiste únicamente el booleano de seguridad entre llamadas, nunca el cuerpo; los turnos legacy abiertos lo reconstruyen desde búsquedas finalizadas y fallan de forma cerrada si el resultado no es auditable.

El resultado completo se entrega al modelo solo dentro del bucle textual del turno y hasta el límite configurado. El prompt obliga a citar la `final_url` realmente descargada y a mostrar también la `source_url` cuando difiere. La actividad persistida no conserva texto ni preview: registra URL de origen/final, consulta/proveedor/posición de búsqueda, tipo, longitud en bytes y caracteres, SHA-256 del texto extraído, truncación y cadena acotada de redirecciones. La auditoría se mantiene por debajo de 4.000 caracteres: ante cadenas patológicamente largas guarda recuento y hash y, si hace falta, prefijo/hash de origen, conservando exacta la URL final. Así se puede auditar la procedencia sin convertir el historial ni `conversation.state` en una copia durable de contenidos externos.

## ADR-032: Política declarativa y autorizaciones one-shot durables (2026-07-16)

Cada herramienta declara en el catálogo si es de solo lectura o escritura de base de datos, su tipo de efecto y si requiere aprobación explícita. El arranque falla si una lectura declara efectos o si una mutación no declara `database_write` y `explicit`; por tanto, añadir una herramienta nueva sin política no la convierte accidentalmente en ejecutable. La confirmación se calcula sobre el payload normalizado que realmente usaría el ejecutor y emite una autorización ligada a actor, conversación, mensaje, herramienta y digest. Las invocaciones directas de mutaciones sin esa autorización se deniegan.

El claim, el efecto y el comprobante durable comparten una única transacción corta y bloqueada. Un crash antes del comprobante revierte también el efecto y permite recuperar el mismo intento; después del commit, un replay devuelve el resultado registrado sin repetir la escritura. Cambiar el payload, el actor o el turno invalida el intento. Los bloqueos de adjuntos y contenido web no confiable, la exclusión web de Hermes y la allowlist del turno se evalúan antes de reclamar la autorización, de modo que una denegación no consume el permiso one-shot. Esta decisión sustituye la regla anterior de ADR-020 que consideraba consumida definitivamente una confirmación aunque el efecto no hubiese podido confirmarse.

Las lecturas externas largas de Agent Office no mantienen una `Session` ni locks durante el proveedor. El claim persiste un deadline absoluto anterior al vencimiento de su lease; cualquier worker reanudado o reemplazado conserva ese mismo límite y no puede iniciar una llamada tardía. El supervisor de embeddings limita globalmente procesos y lanzadores, comprueba el deadline también dentro del hijo y solo devuelve el cupo tras muerte y reap confirmados. Si la limpieza es incierta, el intento y el cupo quedan en cuarentena y requieren revisión humana antes de reintentar: se prefiere degradar capacidad de forma acotada a permitir dos propietarios o un efecto duplicado.

## ADR-033: Reconciliación sucesora de la colisión Alembic `0026` (2026-07-17)

La revisión `20260716_0026` publicada en `main`, que cambia a `pending_review` el estado por defecto de nuevas ordenanzas, queda como historia canónica. Otra revisión geográfica llegó a ejecutarse con el mismo ID, junto con las revisiones de adjuntos `0027` y `0028`, en una base persistente. Renumerar o reescribir esos archivos haría ambiguo `alembic_version` y podría repetir DDL, por lo que `0027` y `0028` permanecen inmutables y el `0026` geográfico se conserva byte a byte fuera del grafo activo, con su SHA-256 documentado.

La sucesora `20260717_0029` reconcilia ambos caminos. Antes de modificar nada valida el default jurídico y la huella estructural gestionada —columnas, tipos, nullability, defaults, propiedad de secuencias, checks, claves, RLS e índices válidos— sobre PostgreSQL 17, fijado en Compose y CI. Crea el DDL histórico cuando está totalmente ausente, adopta sin tocar filas cuando coincide exactamente y aborta ante estados parciales o desconocidos. Después fija solo el default de futuras ordenanzas; no reclasifica decisiones existentes.

En una base con la huella geográfica antigua, la primera operación mutante obligatoria es `upgrade 20260717_0029` o `upgrade head`; el entorno bloquea `downgrade` y `stamp` desde los ambiguos `0026`/`0027`/`0028` hasta reconciliar. Después, el downgrade solo elimina un esquema creado por `0029`, espera como máximo cinco segundos por los locks, cuenta con RLS desactivado o falla de forma cerrada y se niega si hay datasets, snapshots o procedencia municipal. Un esquema geográfico adoptado se preserva siempre. Las variantes aún más antiguas donde adjuntos también utilizó `0026`/`0027` requieren auditoría manual de la huella antes de cualquier `stamp`; no se infieren únicamente a partir del número de revisión.

## ADR-034: Barra municipal configurable sobre bloques de contenido (2026-07-29)

> Reconciliada con ADR-048 en **ADR-052**: los apartados que aquí se describen conviven con las áreas fijas, añadidos detrás de ellas.

La pantalla «Ayuntamiento» del diseño (Claude Design, `Pantalla Principal.dc.html`) deja al usuario crear, renombrar, reordenar y borrar libremente los apartados de su municipio, además de poner su escudo, su nombre y la temperatura del día. Se implementa **extendiendo** el `MunicipalWorkspace` existente, no sustituyéndolo: aquél agrega los módulos operativos ya construidos (inventario, mantenimiento, normativa, personal) y esta decisión le añade el cromo editable.

El contenido se modela como **bloques genéricos** (`municipal_blocks`: padre, posición, título, cuerpo y carga libre en JSON) en lugar de tablas tipadas por dominio. Tablas tipadas serían más consultables y validables, pero obligarían a una migración por cada epígrafe nuevo y no admiten el «añade el apartado que quieras» que el diseño da por supuesto. El check de `block_type` ya admite los tipos reservados de contenido (`epigraph`, `section`, `item`) para que la fase siguiente no necesite migración; la API solo crea los dos de navegación. Los apartados creados aparecen como pestañas tras las áreas fijas, de modo que la pantalla conserva una sola navegación.

El contenido cuelga de `Organization`, no de `Municipality`. `Municipality` es dato de referencia **global compartido entre inquilinos**, mientras que la ficha del Ayuntamiento la edita cada inquilino para sí: colgarla del municipio filtraría contenido editable entre organizaciones. El perfil (`municipal_profiles`) guarda nombre mostrado, escudo, interruptor y coordenadas del bloque de temperatura, uno por organización. La organización la indica siempre quien consume la API: el espacio municipal ya sabe cuál está seleccionada, y un superusuario no tiene «primera organización».

El escudo **no** usa el modelo `Document`: `documents.project_id` es `NOT NULL` y su control de acceso es el del proyecto, ninguna de las dos cosas encaja con una imagen de marca de la organización. Reutiliza el servicio de almacenamiento bajo el prefijo `organizations/<id>/brand/`, conservando escritura por trozos, checksum y defensa de path traversal, con lista blanca propia (solo imágenes) y tope propio (2 MiB).

El bloque de temperatura usa **Open-Meteo** consultado **desde el backend** (`app/town_hall/weather.py`), nunca desde el navegador: así el ayuntamiento no expone a sus usuarios a un tercero. Es un punto de egreso externo nuevo, el primero que no es de IA, y por eso queda fuera del gateway (ADR-013), de `speech.py` (ADR-021) y de `web_search.py` (ADR-022), pero se somete a su misma disciplina: por él solo salen el topónimo público —una sola vez, para geocodificarlo, sin el sufijo de provincia, que el buscador no entiende— y sus coordenadas, jamás datos de usuarios, documentos ni conversaciones, y los logs registran solo metadatos. Open-Meteo se elige sobre AEMET por no requerir clave de API ni alta; el dato es orientativo, no oficial. Las coordenadas resueltas se guardan y se invalidan al cambiar la localidad. La temperatura se cachea en memoria 30 minutos, con la misma condición que el limitador de login: mover a Redis antes de ir a multi-worker (ADR-010). Se pide en un endpoint aparte de la pantalla, de modo que una caída del proveedor no impida cargarla: el bloque muestra entonces un guion, nunca una cifra inventada.

## ADR-035: Publicación en dominio público con proxy inverso único (2026-07-30)

El proyecto pasa de correr solo en `localhost` a servirse en un dominio público con datos municipales reales. `docs/arquitectura.md` admitía «contenedores sin hardening de producción (root, un worker, sin TLS); aceptable mientras todo siga en localhost»: esa condición desaparece el día que el DNS apunta al servidor, y con ella la excusa.

La topología no es una preferencia estética, la fuerzan dos hechos. Primero, la cookie de sesión es `httpOnly`, `SameSite=Lax` y **sin `Domain`** (ADR-010): frontend y API tienen que compartir host, así que queda descartado el par `app.dominio` + `api.dominio`, que además exigiría CORS con credenciales entre orígenes. Segundo, la API **no puede vivir en la raíz**: el backend sirve `/admin/users`, `/admin/groups`, `/admin/roles` y `/admin/permissions`, y el frontend sirve las páginas `/admin`, `/admin/usuarios`, `/admin/grupos` y `/admin/roles`. `/admin/roles` colisiona literalmente. Por eso se elige **un solo hostname con el frontend en `/` y la API bajo `/api`**.

El prefijo se monta como lo documenta FastAPI: el proxy lo **elimina** (`handle_path /api/*`) y el backend arranca con `--root-path /api` para que sus redirecciones y su esquema no lo pierdan. Encaja con `frontend/app/lib/api.ts` sin tocar código: `NEXT_PUBLIC_API_BASE_URL=https://<dominio>/api` no es loopback, así que `normalizeApiBaseUrl` lo usa literalmente y las peticiones salen del mismo origen. Como efecto secundario CORS queda casi vestigial; se conserva restringido al origen real como red de seguridad, no como mecanismo principal.

Se elige **Caddy** frente a nginx porque emite y renueva los certificados de Let's Encrypt sin certbot ni cron, y la configuración cabe en un fichero legible. Es el único servicio que publica puertos (80, 443 y 443/udp). El tope de cuerpo se pone **en el borde** (30 MB) porque el límite de 25 MiB de la aplicación se aplica mientras escribe el fichero, cuando Starlette ya ha volcado el multipart completo a `/tmp`: sin corte en el proxy, una sesión válida puede llenar el disco.

`docker-compose.prod.yml` es un fichero **aparte**, no un override: el de desarrollo usa `network_mode: host` y eso no se retira limpiamente en una capa de merge, de modo que el flujo de desarrollo documentado sigue intacto. En producción desaparece la red de host, Postgres y Redis dejan de publicar puertos y quedan en una red `internal: true` sin salida a Internet, todo lleva `restart: unless-stopped` y healthcheck, los contenedores corren sin privilegios con raíz de solo lectura y capacidades retiradas, y Postgres se fija **por digest**, el mismo que ya usaba CI, en vez de un tag flotante.

El backend corre con `--proxy-headers` y `--forwarded-allow-ips` apuntando a una **IP fija** de Caddy en una subred declarada. Sin ello el limitador de login vería la dirección del proxy para todos los clientes y la traza de seguridad registraría siempre la misma IP; con `*`, cualquiera podría falsear `X-Forwarded-For` y esquivar el límite.

`NEXT_PUBLIC_API_BASE_URL` se hornea en el bundle del navegador durante `next build`, así que **cambiar de dominio obliga a reconstruir la imagen del frontend**. La `ENV` de la etapa runner no lo cambia; se deja documentado en el propio Dockerfile porque es el error más fácil de cometer.

Sigue habiendo un solo worker de uvicorn: el limitador de tasa es en memoria por proceso (ADR-010, ADR-015). Escalar horizontalmente exige moverlo a Redis antes, y esa condición no cambia con este despliegue.

El piloto se despliega **en el mismo VPS donde se desarrolla** (Hetzner CPX42, IP
pública propia, puertos 80 y 443 libres). Una máquina dedicada sería más segura y
cuesta unos 4 €/mes, y así se recomienda para cuando el piloto deje de serlo; se
decide compartir porque el servidor ya está pagado y en marcha, y porque los
contenedores de la aplicación consumen menos de 300 MB frente a 15 GB de RAM. El
riesgo que introduce es concreto y se acota con tres medidas: producción se
despliega desde un clon aparte en `/opt/anacleto` —para que un `git checkout` de
desarrollo no altere la configuración de la web en marcha—, usa un proyecto de
Compose propio (`anacleto`) con volúmenes separados, y **nunca se hace `down`, sólo
`stop`**. Esto último no es cosmético: `prune` sólo borra volúmenes que ningún
contenedor referencia, así que mientras los contenedores existan los datos están a
salvo, y `down` es precisamente lo que los deja huérfanos. Queda además prohibido
`docker system prune -a --volumes` en la máquina; para recuperar disco se usa
`docker builder prune`, que es donde está el espacio (unos 75 GB de caché).

## ADR-036: Endurecimiento del backend expuesto a Internet (2026-07-30)

Antes de este cambio el backend tenía **un solo middleware** (CORS), publicaba `/docs` y `/openapi.json` sin autenticar, no emitía ninguna cabecera de seguridad, no tenía más defensa CSRF que `SameSite=Lax`, no configuraba logging —de modo que todo `logger.info`, incluida la telemetría del gateway de IA, se descartaba en silencio— y no registraba ni un solo evento de seguridad. Nada de eso era grave en `localhost` y todo lo es en un dominio público.

**Guardas de arranque.** Con `ENVIRONMENT=production` el backend se niega a arrancar si `SECRET_KEY` es uno de los valores de ejemplo documentados o mide menos de 32 caracteres, si algún origen de CORS no es `https://` o es `*`, o si `ALLOWED_HOSTS` es `*`. El motivo es concreto: el `.env` de desarrollo se copia con facilidad y llevaba `SECRET_KEY=change-me-...`, `BOOTSTRAP_ADMIN_TOKEN=dev-bootstrap-token` y orígenes en claro; el fallo tenía que ser ruidoso y no silencioso. `environment` se valida además contra un enum **sin normalizar**: aceptar `Development` como equivalente de `development` habría ablandado la puerta de ADR-024, que exige la cadena exacta para el puente Codex.

**Superficie.** `/docs`, `/redoc` y `/openapi.json` solo existen en desarrollo: describían las ~132 rutas, sus esquemas y la cabecera del token de bootstrap sin pedir credenciales. Se añade `TrustedHostMiddleware` con los hostnames reales.

**CSRF.** Se añade una comprobación de `Origin` en los métodos de escritura, como segunda capa sobre la cookie. Si llega `Origin` y no es de los nuestros, 403; si no llega, se permite, porque los clientes de API con `Authorization: Bearer`, el webhook de Telegram y el `TestClient` no lo envían mientras un navegador sí lo hace en toda escritura. No es un token sincronizador —seguiría siendo lo más sólido— pero cubre la forma real del ataque sin romper los caminos legítimos existentes.

Los middlewares se escriben como **ASGI puro** y no sobre `BaseHTTPMiddleware`: el asistente responde por SSE y `BaseHTTPMiddleware` se interpone en el streaming.

**CSP.** La política del frontend se sirve desde el proxy y está verificada contra el código: `img-src` admite las teselas de OpenStreetMap que carga Leaflet, `font-src 'self'` basta porque `next/font` sirve IBM Plex desde nuestro host, y el micrófono se permite porque el modo voz lo usa. `script-src` necesita `'unsafe-inline'`: Next.js App Router inyecta scripts inline para hidratar y el script de tema también es inline. Apretarlo a CSP con nonce exige un `frontend/middleware.ts` y **queda como deuda declarada**, no como algo resuelto. Las respuestas de `/api/*` llevan en cambio `default-src 'none'; sandbox`, que neutraliza cualquier fichero servido con un tipo ejecutable.

**Cuatro vulnerabilidades corregidas.** (1) `GET /town-hall/shield` servía el escudo **inline**, sin `Content-Disposition`, con el content-type declarado por el cliente y `image/svg+xml` en la lista blanca: un usuario con `town_hall.edit` podía almacenar un SVG con script que se ejecutaba en el origen de la API con la cookie de sesión en alcance, y al ser una navegación `GET` la cookie `Lax` viajaba igual. Se sirve como adjunto y la extensión de la cabecera se deriva del content-type validado, no del nombre original, que es el que elige quien sube. Se conserva el soporte de SVG porque el frontend lo pinta en un `<img>`, donde no puede ejecutar scripts. (2) El webhook de Telegram **fallaba en abierto**: sin secreto configurado se saltaba la verificación entera. (3) La llamada gRPC a NVIDIA Riva y el subproceso de ffmpeg no tenían límite temporal y podían inmovilizar el único worker. (4) Hermes, embeddings y Azure usaban `urlopen` desnudo, que sigue redirecciones **reenviando la credencial**; el patrón que ya existía para OpenAI se generaliza a un ayudante compartido.

**Límites de tasa.** Solo login y cambio de contraseña estaban limitados; quedaban abiertos los endpoints que cuestan dinero o CPU en cada llamada: turnos de LLM, síntesis de voz, transcripción (subida de 20 MiB más un ffmpeg por petición), subidas e importación de ordenanzas. Se extienden reutilizando el limitador en memoria en lugar de migrar a Redis: con dos usuarios y un solo worker, mover el estado añade un modo de fallo sin beneficio, y la condición de ADR-010/ADR-015 (Redis antes de multi-worker) sigue en pie. Se añade un **techo por IP** en el login: la clave anterior incluía la cuenta, así que una sola IP podía probar una contraseña contra cuentas distintas sin límite alguno.

**Traza de auditoría.** `security_events` es inmutable por trigger en la base, con el mismo patrón que `maintenance_order_events`, y cubre `UPDATE` **y** `DELETE`: una traza que la propia aplicación pueda reescribir no sirve como prueba, y un backend comprometido no debe poder borrar su rastro. Se instrumentan una decena de puntos de alto valor (login correcto, fallido y bloqueado, logout, cambio y reseteo de contraseña, creación del admin de bootstrap, alta y baja de superusuario, borrado de usuario, subida, descarga y archivado de documentos) en lugar de las 132 rutas. El acceso a documentos es lo que hace defendible el piloto con datos reales. `detail` guarda solo metadatos, nunca contenido: la regla dura del proyecto se mantiene. Se lee por un endpoint **solo de superusuario** y fuera del catálogo RBAC, porque la traza cruza organizaciones —un login fallido no tiene organización— y porque así ningún rol municipal puede concederse acceso a sí mismo. Guarda IP y correo intentado, que son datos personales: la retención es de 90 días con purga programada.

## ADR-037: Copias de seguridad en el servidor con snapshots del proveedor (2026-07-30)

No había ninguna copia de seguridad: ni script, ni cron, ni procedimiento de restauración. La única guía era la prohibición de borrar los volúmenes.

Se elige el esquema **más simple que funciona** para un piloto de dos usuarios: volcado diario en el propio servidor con rotación (7 diarias, 4 semanales, 3 mensuales) más los snapshots automáticos del proveedor de VPS. Se descarta por ahora el destino externo cifrado, que sería más sólido, y **el riesgo residual se acepta explícitamente**: con las copias en la misma máquina, un borrado accidental o un compromiso del servidor se las lleva también. Los snapshots del proveedor son la única red fuera de la máquina, así que **activarlos es obligatorio**, no opcional. La costura para el destino externo queda preparada (una variable y el hueco para cifrar con `age` y subir), sin implementar, para no dejar la ilusión de una protección que no existe.

Al decidirse que producción comparte máquina con el desarrollo (ADR-031), la copia
externa deja de ser un lujo: las copias locales protegen frente a un error de la
aplicación, pero no frente a perder o vaciar el servidor, y en esa máquina se
trabaja a diario. Por eso los backups o snapshots de la consola de Hetzner pasan de
recomendables a **obligatorios**, y son la única copia real fuera del disco.

La base se vuelca con `pg_dump -Fc` y los documentos se empaquetan con el volumen montado en **solo lectura**: el script no puede escribir en los datos de producción ni por error. Cada copia lleva manifiesto `sha256`, y la restauración lo verifica **antes** de tocar nada. La copia se construye en un directorio `.partial` y se renombra solo al terminar bien, para que una ejecución interrumpida no deje un volcado vacío que la rotación contaría como válido desplazando a uno bueno.

`ops/restore.sh` restaura **por defecto a una base de pruebas** y no toca producción, de modo que el ensayo pueda hacerse cuando se quiera; el modo destructivo exige una confirmación escrita y que backend y worker estén parados. Esto es deliberado: una copia que nunca se ha restaurado no es una copia, es una suposición, y el ensayo es parte del procedimiento de puesta en marcha, no una recomendación.

El fichero de entorno se **parsea**, no se ejecuta con `source`: contiene valores sin comillas con espacios (`APP_NAME=Asistente Ayuntamientos`) que reventaban al interpretarlos como shell, y ejecutar un fichero de secretos como código es innecesariamente peligroso.

La purga de `security_events` desactiva el trigger de inmutabilidad de forma explícita dentro de una transacción y comprueba después que quedó activo. Es la única excepción prevista a la inmutabilidad y por eso vive en una tarea de mantenimiento, fuera de la aplicación.

## ADR-038: El tiempo se consulta en vivo; el escudo vive en `documents` (2026-08-04)

**Open-Meteo sin tabla.** El tiempo que hace ahora no es un dato del
ayuntamiento: es una lectura de fuera que caduca en minutos. Guardarla obligaría
a decidir cuándo purgarla y a convivir con una base que afirma que hacen doce
grados desde hace tres semanas. Se pide en vivo y se cachea en Redis treinta
minutos, que es el orden en que el proveedor actualiza; sin caché, un municipio
con varias personas trabajando generaría decenas de peticiones por minuto contra
un servicio gratuito.

Si Redis no responde, la consulta sigue adelante sin caché en lugar de fallar, y
si el proveedor no responde el endpoint devuelve 503 y el bloque no se dibuja. El
tiempo es contexto, no un dato del que dependa ninguna decisión municipal: nunca
debe tumbar la pantalla.

**El host lo fija el código, y aun así se verifica.** `api.open-meteo.com` es una
allowlist de un solo elemento y ninguna parte de la petición viene del usuario,
pero eso no basta: un DNS comprometido podría resolver ese nombre a una dirección
interna. Siguiendo el patrón de `assistant/web_reader.py`, se resuelve primero,
se exige que **todas** las direcciones devueltas sean públicas —basta una interna
entre varias para abortar— y se conecta contra la dirección ya validada
conservando el SNI, de modo que el certificado se comprueba contra el host real.
La respuesta tiene tope de tamaño y una lectura sin temperatura o sin instante se
descarta entera.

**El escudo es una referencia, no un fichero nuevo.** `organization_branding`
guarda el id de un documento ya subido, no bytes ni una ruta suelta: los ficheros
de este producto viven en `documents`, con su control de acceso, su checksum y su
ciclo de archivado, y estrenar un segundo almacén habría significado reimplantar
todo eso. La clave primaria es la propia organización, porque no caben dos
escudos. El documento debe pertenecer a la misma organización: si no, bastaría
conocer un id ajeno para colgar la imagen de otro municipio.

**Los contadores de agua no apuntan a `municipalities`.** La clave ajena directa
existía en la primera versión y la CI la rechazó: soltar la tabla en un downgrade
pedía un lock exclusivo sobre `municipalities`, y un solo escritor abierto bastaba
para que la bajada esperase en vez de fallar rápido, que es justo lo que vigila
`test_legacy_geography_downgrade_rejects_without_waiting_for_writer`. La
integridad no cambia: la clave compuesta hacia `organizations(id,
municipality_id)` ya obliga a que el municipio sea el de la organización, y esa
columna apunta a su vez a `municipalities`. La lección es que una clave ajena
redundante no es gratis: se paga en los locks del downgrade.
## ADR-039: Gráficas propias en SVG y degradación silenciosa (2026-08-04)

Las series del municipio se dibujan con dos componentes SVG escritos aquí,
`LineChart` y `BarChart`, en lugar de incorporar una librería de gráficas. Son
series de pocos puntos —un valor por año o por mes— con dos formas: una línea
para lo continuo y unas barras para lo discreto. Cualquier librería del ramo pesa
más que toda la pantalla que la usaría, y traería su propio modelo de temas justo
cuando ADR-048 acaba de fijar que lo visual va en CSS Modules con los tokens del
producto.

El pie visible de la figura es también el nombre accesible del SVG,
mediante `aria-labelledby`; la primera versión repetía el título dentro de un
`<title>` y un lector de pantalla lo habría anunciado dos veces.

**Los adornos informativos degradan en silencio.** El bloque de temperatura de la
barra superior y las series de la ficha municipal comparten una regla: cuando su
consulta falla, no se dibujan y no levantan bandera de error. Ninguno de los dos
sostiene una decisión municipal, y un aviso rojo en la cabecera institucional le
daría a una avería del servicio del tiempo el mismo peso visual que a un problema
del ayuntamiento. Se distingue lo vacío de lo roto donde importa —el inventario,
la plantilla, la hoja de ruta avisan— y se calla donde no.

Las series se piden siempre, sin condicionarlas a un permiso en el cliente: si la
cuenta no tiene `municipal_data.view`, la petición vuelve con 403 y el bloque
enseña su estado vacío, que dice lo mismo sin duplicar la regla de autorización
en dos sitios.
## ADR-040: Administración y comunicación, con la publicación como transición (2026-08-05)

La administración municipal entra en `backend/app/administration/` —horarios de
atención, trámites, licencias, contratos, subvenciones y publicidad activa— y la
comunicación en `backend/app/communications/`. Se separan porque tienen dueños
distintos: la administración la lleva la secretaría y la comunicación, alcaldía.
Compartir permisos habría obligado a que quien redacta un bando pudiera tocar
expedientes de licencia.

**Publicar un bando es una transición, no un campo.** Un bando nace en borrador
y se expone mediante `POST /notices/{id}/publish`, con `communications.publish`,
distinto de `communications.edit`. Redactar y exponer son actos diferentes:
exponer produce efectos administrativos y suele corresponder a otra persona.
Retirarlo exige motivo y **no borra `published_on`**, porque que el bando llegó a
estar expuesto en esa fecha puede tener que demostrarse después; queda además el
evento en `municipal_notice_events`, append-only. Reexponer lo retirado se
rechaza: sería reescribir la historia, y lo correcto es publicar uno nuevo.

Las reglas que expresan una verdad del dominio viven en la base, no solo en
Pydantic. Una licencia resuelta tiene fecha de resolución y una sin resolver no
—un `CHECK` con `(status in ('granted','denied')) = (resolved_on is not null)`—;
un contrato adjudicado tiene adjudicatario e importe; una noticia publicada dice
desde cuándo. Son afirmaciones que no dependen de qué endpoint escriba la fila.

Los horarios guardan **minutos desde medianoche** en lugar de `TIME`. Comparar y
ordenar franjas se vuelve aritmética simple y no arrastra la zona horaria que un
`TIME WITH TIME ZONE` obligaría a razonar; la interfaz los formatea al leerlos.
El orden de la semana se aplica al servir, porque alfabéticamente «friday» iría
antes que «monday» y así no lee un horario nadie.

Las referencias de expediente son únicas **por organización**, no globalmente:
los ayuntamientos numeran sus expedientes por su cuenta y dos municipios pueden
tener legítimamente el mismo `LIC-2026-01`.

`publish_to_sede` aparece ya en trámites, transparencia, noticias y bandos, sin
consumidor todavía. La sede electrónica de la fase 7 será read-only sobre estas
tablas, y la bandera es el contrato que necesita para saber qué sale al público:
declararla ahora evita una migración que toque cuatro tablas más adelante.

La revisión Alembic `20260805_0038` se serializa detrás de `20260804_0037`
conforme a ADR-033.
## ADR-041: Presupuesto derivado y tesorería independiente (2026-08-05)

`backend/app/budgets/` guarda el presupuesto anual, sus partidas, las
modificaciones de crédito, los gastos imputados y los movimientos de tesorería;
`backend/app/plenos/`, las sesiones del pleno y su orden del día.

**La ejecución presupuestaria no se guarda.** Ni el gasto ejecutado ni el
crédito disponible son columnas: se calculan en cada consulta sumando partidas,
modificaciones aprobadas y gastos. Guardarlos obligaría a recalcular en cada
escritura y a convivir con un total que dejó de cuadrar tras un fallo a medio
camino. Es el mismo criterio que «vencida» en ADR-050: un dato calculable que se
almacena empieza a envejecer en cuanto cambia el que lo origina.

**El importe de una partida es siempre positivo** y la dirección la marca
`kind` (`income` o `expense`), para que sumar ingresos y gastos por separado no
dependa de leer bien un signo. En las modificaciones, en cambio, el signo sí
importa: una modificación de crédito puede retirarlo.

**Un presupuesto en borrador se edita; uno aprobado se modifica.** Sobre un
borrador se cambia la partida directamente y no caben modificaciones de crédito,
porque no hay nada aprobado que modificar. Y una modificación solo mueve crédito
cuando está aprobada —aprobarla pide `budgets.manage`—; en borrador es una
intención, y la ejecución no la cuenta.

**La tesorería no cuelga del presupuesto**, a propósito. El dinero entra y sale
con su propio calendario: una factura puede imputarse a una partida de un año y
pagarse en el siguiente. Atar `treasury_movements` a un presupuesto obligaría a
mentir en una de las dos fechas o a inventar una imputación que nadie ha hecho.

En los plenos, el acta es un documento de `documents` y no texto suelto, igual
que el escudo en ADR-038. Aprobarla pide `plenos.manage` mientras que redactarla
se queda en `edit`: aprobar es el acto que convierte el acta en el registro
oficial de lo acordado. Un `CHECK` garantiza que un acta aprobada tiene
documento, y que una sesión cancelada no produce acta ni orden del día, porque
no llegó a celebrarse.

Los votos de un punto del orden del día son **nulos mientras no se vota**: un
punto informativo no tiene votación, y cero votos a favor no es lo mismo que no
haberse votado. Un `UNIQUE` por sesión y posición mantiene el orden del día sin
huecos ambiguos.

La revisión Alembic `20260805_0039` se serializa detrás de `20260805_0038`
conforme a ADR-033, y ninguna tabla nueva referencia `municipalities` en directo,
por lo aprendido en ADR-038 sobre los locks del downgrade.
## ADR-042: La sede electrónica agrega, no duplica (2026-08-05)

`backend/app/sede/` no guarda casi nada. Bandos, noticias, trámites,
transparencia, contratos, plenos y normativa ya viven en sus dominios, y la sede
sólo recoge lo que lleva `publish_to_sede`. Copiar esos datos a tablas propias
habría creado dos verdades que envejecen por separado: retirar un bando dejaría
de notarse en el portal, que es justo el fallo que un tablón no se puede
permitir. El único dominio nuevo es `municipal_taxes`, porque un tributo no era
ninguna de las cosas anteriores.

**Publicar es una condición, no una copia.** Cada sección filtra por su propio
criterio de «esto ya es público»: un bando debe estar `published` —un borrador o
uno retirado no están expuestos, aunque consten en el histórico interno—, un
contrato debe haber salido a licitación, y una ordenanza debe estar vigente y
curada, porque la sede no es sitio para normativa pendiente de revisar. La
respuesta lleva `Cache-Control: no-store`: retirar un bando tiene que notarse de
inmediato.

**Sigue exigiendo autenticación.** Es la vista previa de lo que verá la
ciudadanía, no el portal público. Abrirla sin sesión habría expuesto por
comodidad datos cuya publicación real es una decisión de despliegue —dominio,
cabeceras, indexación— y no de este endpoint. Cuando exista ese portal, podrá
consumir esta misma agregación.

**Todo llega en una respuesta.** La sede de un municipio pequeño cabe de sobra, y
siete peticiones para pintar siete pestañas serían siete comprobaciones de
permiso y siete viajes para lo mismo. Cambiar de pestaña no vuelve a pedir nada.

Un tributo puede existir sin su ordenanza fiscal digitalizada, así que
`ordinance_id` es opcional. La cuota se expresa como tipo, importe fijo o tarifa,
y un `CHECK` obliga a que los dos primeros traigan su número y la tercera su
descripción: un tipo porcentual sin valor no dice cuánto se paga.

La revisión Alembic `20260805_0040` se serializa detrás de `20260805_0039`
conforme a ADR-033.
## ADR-043: Taxonomía de partida del inventario, sembrada bajo petición (2026-08-05)

El árbol de capas del diseño —vías, agua, saneamiento, alumbrado, mobiliario,
parques, residuos, seguridad, deportivas, espacios públicos, cementerio,
vehículos— no se modela como catálogo del producto sino como **semilla** en
`backend/app/assets/seed.py`. Un ayuntamiento recién dado de alta que abre el
mapa y encuentra un formulario vacío no sabe qué contestar; uno que encuentra
trece categorías con sus tipos habituales sí, y a partir de ahí adapta.

**Se siembra bajo petición, no al arrancar.** `POST /assets/taxonomy/seed` con
`assets.manage`, por organización. Hacerlo automático en el `lifespan` habría
impuesto la taxonomía a organizaciones que no la quieren y habría reintroducido
categorías que alguien archivó a conciencia, cada vez que el servicio reinicia.

**Y no reescribe nada.** El seed es idempotente y no destructivo: una categoría
que ya existe se deja como esté —renombrada, con otro color o archivada— y sólo
se completan los tipos que falten. Un ayuntamiento que llamó «Aguas del
municipio» a su categoría de agua no debe encontrársela revertida tras un
redespliegue; que archivó el cementerio porque lo lleva una junta vecinal,
tampoco. Hay tests para ambos casos.

Los códigos del seed se validan contra el mismo `CHECK` que impone la base
—minúsculas ASCII con guiones—, en un test que recorre la tabla entera. La
primera versión traía `frontón` con tilde y habría reventado la inserción en
producción sin que ningún test de dominio lo notara.

Esta es la parte de backend de la fase del mapa general. La pantalla
`MapaGeneral.tsx` va aparte, y **no toca `MapPanel.tsx` ni `SiurLayerTree.tsx`**:
esos dos ficheros aparecen en tres PRs abiertos sin mergear (#15, #16, #28) y
editarlos garantizaría un conflicto. `MunicipalMap.tsx` sí se reutiliza, porque
su interfaz de props es estable y nadie la está tocando.
## ADR-044: El árbol de capas se deriva de lo que hay en el mapa (2026-08-05)

`MapaGeneral.tsx` entra como sub-pestaña de la pantalla del ayuntamiento y
**construye su árbol de capas desde los propios elementos**, agrupando por el
`layer_key`, `layer_label` y `layer_color` que `/geo/map-items` ya devuelve. No
hay un catálogo de capas aparte que mantener sincronizado, y una capa sin nada
dentro sencillamente no aparece: un árbol con doce ramas vacías no informa, sólo
obliga a buscar.

El filtrado —capas apagadas y búsqueda— es local. El mapa se carga entero una
vez y apagar una capa o teclear en el buscador no vuelve a pedir nada al
servidor, porque el inventario de un municipio pequeño cabe en memoria y la
alternativa sería una petición por cada tecla.

**No se tocan `MapPanel.tsx` ni `SiurLayerTree.tsx`.** Aparecen en tres PRs
abiertos sin mergear (#15, #16, #28) y editarlos garantizaría un conflicto en
ficheros de 2.781 y 400 líneas. `MunicipalMap.tsx` sí se reutiliza tal cual: su
interfaz de props es estable, acepta `items`, `markerColors` y `selectedItemId`,
y con eso basta para una vista propia. El resultado es que la fase del mapa
—señalada como la de mayor riesgo en el plan— no modifica ni una línea del
código en churn.

Con «Mapa general» activo, **el menú fijo de ADR-048 queda completo**: todas las
entradas declaradas tienen pantalla. `visibleTopNavSections` sigue filtrando por
`enabled` aunque hoy no descarte nada, porque es lo que protegerá el día que se
declare una entrada nueva antes de construirla.
## ADR-045: Patrimonio y archivo, separados del inventario (2026-08-05)

`backend/app/heritage/` guarda los bienes patrimoniales y las piezas del archivo
municipal. Se separa de `municipal_assets` a propósito: aquel dominio existe para
**mantener** cosas —una farola se repara y se sustituye— y este para
**conservarlas**. Una ermita del XVI y una luminaria no comparten ciclo de vida
ni vocabulario, y mezclarlas obligaría a que cada consulta de mantenimiento
filtrase lo que no debe tocar.

**La época va en texto libre.** «Siglo XVI», «finales del XIX o principios del
XX», «indeterminada». Forzar un año o un rango numérico sería inventar precisión
que la fuente no tiene, y llenaría la base de fechas aproximadas que después
alguien leería como exactas.

**Sin declarar es una respuesta legítima.** `protection_level` admite `none`
porque mucho patrimonio de un pueblo es valioso sin figura de protección; lo que
sí exige un `CHECK` es que un bien declarado traiga la referencia de su
declaración, porque sin expediente la declaración no consta.

En el archivo, la **signatura y la ubicación física son lo primero**, no un
adorno: un archivo de pueblo vive en cajas y estantes, y lo que más se busca es
dónde está el papel. La búsqueda incluye `physical_location` por eso mismo. Los
años se guardan sueltos —`start_year`, `end_year`— en vez de fechas, porque de
una caja se conoce el periodo que abarca y casi nunca el día; «sin fechar» es una
respuesta que la ficha da sin fingir un intervalo.

**Digitalizado significa que el fichero existe.** Un `CHECK` exige el documento
cuando el estado es `digitised`; `in_progress` sí puede no tenerlo todavía. Sin
esa regla, «digitalizado» acabaría siendo una promesa que nadie puede comprobar.
El escaneo vive en `documents`, como el escudo en ADR-038 y las actas en ADR-041.

La revisión Alembic `20260805_0041` se serializa detrás de `20260805_0040`
conforme a ADR-033.

## ADR-046: Las piezas de la conversación salen del panel del asistente (2026-08-05)

`AssistantPanel.tsx` había llegado a 2854 líneas y a 52 hooks en un único
componente. La fase 9 del rediseño no toca su comportamiento: mueve a
`frontend/app/components/asistente/conversationParts.tsx` las 500 líneas que
estaban **antes** del componente y que no dependían de su estado —agrupar
conversaciones por fecha o por carpeta, formatear fechas y tamaños, y pintar la
cronología de acciones, el markdown y las tarjetas de adjunto—. El cuerpo movido
es idéntico línea a línea al original; lo único que cambia es el `export` y el
lado del `import`.

**Por qué no se trocea el componente.** Repartir 52 hooks entre varios ficheros
obligaría a subir estado o a inventar un contexto, y eso sí sería un cambio de
comportamiento disfrazado de limpieza. Un componente grande con estado
entrelazado se refactoriza cuando hay una razón funcional para hacerlo, no para
bajar una cifra de líneas.

**Lo que se gana es que ahora se puede probar.** Estas piezas eran inalcanzables
desde un test sin montar el panel entero con su red y sus streams; ahora tienen
14 tests propios que fijan cosas que antes nadie comprobaba: que una conversación
archivada de hoy va a «Archivadas» y no a «Hoy», que una fecha ilegible cae al
fondo en vez de romper el reparto, que borrar una carpeta no hace desaparecer sus
conversaciones —caen a «Sin carpeta»—, y que un adjunto de 1 byte no se muestra
como 0 KB.

No hay dependencias nuevas ni componentes nuevos: el módulo importa lo mismo que
importaba el bloque, y el panel dejó de importar los catorce símbolos que sólo
usaba ese bloque.

## ADR-047: Un despliegue con valores de desarrollo no arranca (2026-08-18)

`SECRET_KEY` tenía como valor por defecto `change-me-in-development`, y nada
comprobaba que cambiase. Esa clave firma los JWT, las autorizaciones de
herramientas y las firmas del catálogo de ordenanzas; su valor por defecto está
publicado en este repositorio. Un despliegue que arrancase con él permitiría a
cualquiera fabricarse un token de administrador. Lo mismo, en menor grado, con
el token de bootstrap —que crea el primer superusuario—, la contraseña de la
base de datos y unos orígenes CORS apuntando a `localhost`.

**Fallar al arrancar, no degradarse.** Cuando `ENVIRONMENT` no es `development`
ni `test`, el validador de `Settings` rechaza esos cuatro valores y el proceso
no levanta. Un aviso en el registro no sirve: nadie lee los registros de un
servicio que responde, y una plataforma municipal firmando tokens con una clave
pública no es un modo degradado, es una puerta abierta.

**Se comprueban literales, no entropía.** La lista contiene exactamente las
cadenas que aparecen en `.env.example` y como valores por defecto, más un
mínimo de 32 caracteres para la clave de firma. Inventar una heurística de
aleatoriedad daría falsos positivos con claves legítimas y falsos negativos con
`password123`; lo que de verdad ocurre es que alguien copia la plantilla sin
leerla.

**Todos los problemas se informan a la vez.** Arreglar un despliegue a base de
reinicios, descubriendo un fallo por vuelta, es la forma más rápida de que
alguien se rinda a medias y deje dos valores sin cambiar.

**CORS vacío es legítimo.** *(Superado por ADR-036 al integrar la línea del servidor: allí la lista explícita es obligatoria en producción, y su `.env.production.example` ya la trae. Lo que sigue describe el razonamiento original.)*  ADR-010 pone frontend y backend bajo el mismo host,
donde no hay petición cross-origin que permitir. Exigir una entrada sería pedir
ruido. Lo que sí se rechaza es un origen de loopback o en texto plano: la cookie
de sesión se emite `Secure` fuera de desarrollo, así que un origen `http://` no
puede sostener una sesión aunque se le autorice.

La documentación interactiva de la API (`/docs`, `/redoc`, `/openapi.json`) pasa
a servirse solo en desarrollo por la misma razón: describe cada endpoint, cada
esquema y cada permiso, y los endpoints siguen exigiendo autenticación, pero no
hay motivo para regalar el mapa.

Los contenedores dejan de correr como root. Mantienen **un solo worker** de
uvicorn: los limitadores de peticiones siguen siendo por proceso (ADR-015) y
añadir workers los desactivaría de hecho. `docs/despliegue.md` recoge el
procedimiento completo y lo que sigue abierto.

## ADR-048: Barra superior municipal fija y tipografía institucional (2026-08-03)

> Reconciliada con ADR-034 en **ADR-052**: la barra superior sigue fija; la tira de pestañas de la pantalla Ayuntamiento admite apartados propios.

El diseño municipal de referencia introduce una barra superior propia de las
pantallas institucionales: escudo y nombre del municipio a la izquierda,
navegación por secciones en el centro y un bloque de contexto a la derecha. Esa
barra no sustituye al menú lateral de ADR-014, que sigue siendo la navegación
del producto; convive con él y solo aparece en las rutas del ayuntamiento, la
sede electrónica y la hoja de ruta. El resto del producto se navega igual que
antes, de modo que un usuario sin acceso a las pantallas municipales no ve
ningún cambio estructural.

La navegación municipal es fija y vive en código (`frontend/app/lib/topNav.ts`).
El diseño incluía un editor que permitía renombrar, reordenar, añadir y eliminar
apartados desde la interfaz, y se descarta de forma deliberada: el menú de un
ayuntamiento describe su organización, no una preferencia de quien lo mira, y
mantenerlo declarado en el repositorio lo hace revisable, comparable entre
municipios y consistente con el modelo de permisos. Las entradas cuyo destino
todavía no existe se declaran con `enabled: false` y no se renderizan; sirven de
índice de lo que falta y se activan en la fase que construye su pantalla, sin
reescribir el modelo ni dejar enlaces rotos en producción.

Se añade **Newsreader** vía `next/font` para el nombre del municipio, único uso
de serif institucional en la barra. El diseño empleaba además Space Grotesk y
Space Mono en detalles puntuales; no se incorporan, porque IBM Plex Sans y Mono
—ya cargadas y autoalojadas— cubren esos usos sin ampliar el peso tipográfico
que el navegador debe descargar. La decisión es reversible: si una revisión
visual las echa en falta, añadirlas es un cambio local en el layout raíz.

Las pantallas nuevas o rediseñadas usan CSS Modules, no `styles.css`. La hoja
global supera las 6.000 líneas y concentra el riesgo de colisión entre cambios
simultáneos; los módulos ya son el patrón de las superficies recientes. En
`styles.css` solo se añaden tokens compartidos: `--status-blocked` y
`--status-overdue`, que los estados de la hoja de ruta necesitan en ambos temas
y que no pueden expresarse con `--danger-fg` o `--accent` sin perder significado.
## ADR-049: Dominios tenant-scoped de gobierno y personal (2026-08-03)

La pantalla del ayuntamiento necesita dos realidades que hasta ahora no existían
en el modelo: quién gobierna el municipio y quién trabaja en él. Se separan en
dos dominios, `backend/app/government/` y `backend/app/staff/`, porque responden
a preguntas distintas y se rigen por reglas distintas. La corporación —alcaldía,
tenencias, concejalías y secretaría— es información pública que acaba en la sede
electrónica; la plantilla es información laboral, sensible, de acceso mucho más
restringido. Mezclarlas en una tabla de «personas del ayuntamiento» habría
obligado a filtrar por rol en cada consulta y a razonar sobre la confidencialidad
caso por caso.

Ambos dominios se anclan a `organizations`, no a `municipalities`. La
organización es la unidad de aislamiento del producto y la que ya sostiene el
modelo de permisos; el municipio describe el territorio. Un cargo o un puesto se
pueden registrar antes de que el municipio esté dado de alta, así que —a
diferencia del inventario, que sí exige municipio para poder situar un activo en
el mapa— aquí las escrituras sólo requieren que la organización esté activa. Las
lecturas se admiten también con la organización pausada, en modo consulta, igual
que en el resto de superficies municipales. Las claves ajenas compuestas
`(id, organization_id)` impiden que un puesto cuelgue de la plantilla de otro
ayuntamiento o que una ausencia se enganche a una persona ajena: el aislamiento
se sostiene en la base, no sólo en el filtro de la consulta.

La plantilla se modela como puesto y persona separados, porque el puesto
sobrevive a quien lo ocupa: en un municipio pequeño el arquitecto puede estar a
tiempo parcial, vacante o compartido con otro ayuntamiento, y el histórico debe
seguir siendo legible cuando cambia el titular. `staff_posts` admite además
contenedores que agrupan puestos sin poder ocuparse, para reproducir la
estructura por servicios del diseño. Un puesto lo ocupa como mucho una persona a
la vez, garantizado por índice único; los ciclos del árbol se cierran en la ruta,
porque la base sólo puede impedir que un puesto sea su propio padre.

Los permisos siguen la gradación del inventario, adaptada a lo que cada dominio
puede sufrir. Gobierno usa `government.view` y `government.manage`: la
corporación cambia en bloque tras unas elecciones, no campo a campo, y no
justifica un nivel intermedio. Personal usa `staff.view`, `staff.edit` y
`staff.manage`: corregir un teléfono, anotar una ausencia o abrir un parte de
trabajo es rutina diaria y vive en `edit`, mientras que tocar la estructura de
puestos o archivar a una persona —que la retira de las vistas— exige `manage`.
Los cargos y las fichas no se borran: se archivan, para que actas, acuerdos y
partes antiguos sigan siendo interpretables.

Toda modificación de una ficha de personal deja un evento en
`staff_history_events`, append-only. Son datos laborales y su edición tiene que
poder auditarse: un cambio de estado, de puesto o de campos queda registrado con
su autor y su momento, en lugar de sobrescribirse en silencio. El diario y los
partes (`staff_reports`) y las facturas del personal externo (`staff_invoices`)
cuelgan de la persona y heredan su organización; facturar sólo se admite en
quien está marcado como externo, porque en alguien de nómina sería casi siempre
un error de captura.

La revisión Alembic `20260803_0034` se serializa detrás de `20260717_0033`
conforme a ADR-033, y `test_migrations.py` fija la huella estructural de las
siete tablas nuevas en las dos direcciones del grafo.
## ADR-050: La hoja de ruta y el estado "vencida" como lectura, no como dato (2026-08-04)

La hoja de ruta municipal necesita un dominio propio, `backend/app/tasks/`, con
`municipal_tasks` y su rastro append-only `municipal_task_events`. No se apoya en
`maintenance_orders` porque aquel dominio existe para el mantenimiento de un
activo concreto y exige uno; buena parte del trabajo de un ayuntamiento pequeño
no cuelga de ningún activo ni de ningún expediente. Una tarea puede referirse a
un proyecto y asignarse a alguien de la plantilla, pero ninguna de las dos cosas
es obligatoria, y ambas se atan con claves ajenas compuestas
`(id, organization_id)` para que no crucen de ayuntamiento.

**"Vencida" no es un estado ni una columna.** El diseño la presenta junto a
"bloqueada" o "en curso", pero no es de la misma naturaleza: bloqueada describe
una decisión de alguien, vencida solo dice que la fecha límite ya pasó y la
tarea sigue abierta. Materializarla obligaría a un proceso que reescribiese
filas cada medianoche, y entre ejecución y ejecución la base contendría datos
que ya no son ciertos. Se calcula en la consulta contra `current_date` del
servidor, y `GET /tasks/summary` devuelve además el `reference_date` que ha
usado, para que la interfaz decida con la misma fecha que el backend y no con el
reloj del navegador. `test_migrations.py` comprueba que la columna no existe, de
modo que un futuro intento de guardarla no pase inadvertido.

El grafo de transiciones es explícito y una tarea cerrada no se edita: se reabre
a `pending` y desde ahí vuelve a moverse. Así la reapertura queda en el
histórico en lugar de disimularse como un salto directo. Bloquear exige motivo
—una tarea bloqueada sin decir qué la bloquea no la puede desatascar nadie, y un
`CHECK` lo garantiza en la base—, y cancelar o reabrir exigen explicación,
porque borran o revierten una decisión anterior. Cancelar y reabrir piden
`tasks.manage`; el resto del movimiento diario vive en `tasks.edit`.

La pantalla deja de ser una pestaña de `/ayuntamiento` y pasa a ruta propia
`/hoja-de-ruta`, con la entrada de la barra superior apuntando ahí. Cruza
tareas, proyectos y corporación, y no cabe dentro de la ficha de un municipio.
La revisión Alembic `20260804_0035` se serializa detrás de `20260803_0034`
conforme a ADR-033.
## ADR-051: Series municipales propias junto a las cifras oficiales (2026-08-04)

La pantalla del municipio necesita empadronamiento, clima, parque de viviendas y
abastecimiento de agua. Todo eso vive en `backend/app/municipal_data/`, separado
de `municipalities`, porque responde a una pregunta distinta: `municipalities`
guarda la ficha oficial del municipio —una fila, con su procedencia INE y su
huella de descarga—, mientras que estas tablas guardan **series temporales que
mantiene el ayuntamiento**. Meterlas en la ficha habría obligado a decidir qué
año es "el" año.

Padrón municipal y cifra oficial del INE conviven a propósito. El padrón se
cierra antes que la cifra oficial y los ayuntamientos trabajan con él durante
meses; presentarlos como el mismo dato llevaría a discusiones sobre cuál está
mal. Por eso cada fila lleva `source` (`municipal`, `ine`, `aemet`, `other`) y la
serie propia no sobreescribe la del INE que ya resuelve
`municipalities/ine_population.py`.

`climate_records` cubre año y mes en una sola tabla: `reference_month` nulo es el
resumen anual y con mes la fila es mensual. Duplicar el esquema para lo mismo a
dos granularidades habría obligado a mantener dos veces cada validación. La
unicidad es por `(organización, año, mes)`, de modo que la fila anual y las doce
mensuales del mismo año conviven sin chocar.

Los contadores de agua son la única parte que se sitúa en el territorio, así que
son los únicos que exigen que la organización tenga municipio, igual que el
inventario; el resto de series no lo necesita y no lo pide. Las lecturas son una
por contador y día —dos lecturas del mismo día se contradicen— y el consumo se
deriva restando lecturas consecutivas en lugar de guardarse, por la misma razón
que "vencida" no es columna en ADR-050: un dato calculable que se almacena
empieza a envejecer en cuanto cambia el que lo origina.

Los permisos son `municipal_data.view|edit|manage`. No hay `create` separado
porque estas series se rellenan y se corrigen en el mismo gesto —una cifra de
padrón mal tecleada se arregla, no se archiva—, y distinguir crear de editar solo
habría añadido un permiso que nadie concedería por separado.

La revisión Alembic `20260804_0036` se serializa detrás de `20260804_0035`
conforme a ADR-033.

## ADR-052: Áreas fijas en código y apartados propios de cada ayuntamiento (2026-08-24)

Las dos líneas que esta integración une decidieron por separado cómo se navega
la pantalla «Ayuntamiento», y llegaron a conclusiones opuestas. ADR-034 la hizo
**configurable**: cada organización crea, renombra, reordena y borra sus
apartados sobre el árbol genérico de `municipal_blocks`. ADR-048 la declaró
**fija** en el repositorio y descartó de forma explícita ese mismo editor,
porque «el menú de un ayuntamiento describe su organización, no una preferencia
de quien lo mira». Ninguna de las dos conocía a la otra, y las dos están en
producción: una en el servidor, la otra en `main`.

**No son la misma superficie.** ADR-048 gobierna la barra superior de las rutas
municipales (`frontend/app/lib/topNav.ts`, `TopBar.tsx`): qué pantallas existen
y cómo se llega a ellas. ADR-034 gobierna la tira de pestañas *dentro* de la
pantalla Ayuntamiento: qué contiene la ficha de ese municipio. Al leerlas como
si compitieran se pierde que responden a preguntas distintas, y por eso ninguna
de las dos tenía que ceder entera.

**Las áreas operativas siguen fijas y en código.** Información, Normativa,
Servicios municipales, Mapa general, Personal y Hoja de ruta son las seis áreas
de `TAB_DEFINITIONS`. Cada una tiene su modelo, sus permisos y su pantalla; no
son texto que alguien pueda renombrar sin que deje de cuadrar con lo que hay
detrás. El argumento de ADR-048 vale aquí sin matices: revisables, comparables
entre municipios y consistentes con el modelo de permisos.

**Los apartados propios se añaden detrás, nunca en medio.** Lo que la
organización crea en el editor aparece tras las seis áreas fijas, en la misma
tira, de modo que la pantalla conserva una sola navegación. Su identificador es
el de su bloque (`block-<id>`), que no puede colisionar con las claves
literales de las áreas fijas. Un ayuntamiento que no use el editor ve
exactamente la pantalla de ADR-048.

**La frontera es quién responde de cada cosa.** Un área fija promete una
funcionalidad que el producto mantiene; un apartado propio es contenido del que
responde el ayuntamiento que lo escribió. Por eso lo segundo se guarda como
bloques genéricos y lo primero no, y por eso borrar un apartado propio no puede
dejar rota ninguna pantalla: `findTabForBlock` traduce los enlaces antiguos y
una pestaña que ya no existe se explica en su panel en lugar de romper.

**La pestaña sigue viviendo en la URL.** ADR-034 la guardaba en estado local y
la escribía con `history.replaceState`; se adopta el criterio de `main`, que la
deriva de `?tab=` con `useSearchParams`. Es la convención del repositorio —los
enlaces profundos y el botón atrás deben seguir funcionando— y es lo que ya
esperaba `UrlDrivenTabs.test.ts`. Los apartados propios se aceptan por su forma
al leer la URL, porque el árbol del menú todavía no ha llegado en ese momento.

**Lo que no cambia.** El escudo, el nombre mostrado y el bloque de temperatura
de ADR-034 se conservan tal cual, incluida su disciplina de egreso hacia
Open-Meteo y el guion cuando el proveedor no responde. `topNav.ts` no se toca:
la barra superior de ADR-048 sigue siendo fija.

## ADR-053: Estructura de partida del Ayuntamiento, sembrada bajo petición (2026-08-31)

Un ayuntamiento recién dado de alta abre la pantalla «Ayuntamiento» y encuentra
una fila de pestañas vacía. Sabe que puede crear apartados —tiene el editor de
ADR-052— pero no cuáles, y el coste de arranque cae entero sobre el primer
usuario. El diseño de referencia sí sabe cuáles, así que la estructura de partida
se toma de él y se siembra, con el mismo patrón que ADR-043 usó para la taxonomía
del inventario: `backend/app/town_hall/seed.py` y
`POST /town-hall/structure/seed`.

**Los nombres no se inventan.** Salen del proyecto exportado de Claude Design:
el orden de los epígrafes es su `infoDefault`, sus títulos son `infoDefTitles` y
las pestañas internas de cada uno salen de su registro de `applyTabOv`.

**El diseño tiene un nivel más que el modelo**, y hay que colapsar uno: allí es
pestaña → epígrafe → pestaña interna → contenido, y aquí pestaña → apartado →
elemento. Se colapsa el del epígrafe, porque cada pestaña interna suya trae un
formato distinto —la demografía es una serie, el análisis de agua son ficheros,
los teléfonos son contactos— y un apartado sólo admite un formato. Así que el
epígrafe con pestañas internas se convierte en pestaña y sus pestañas internas en
apartados; los que no las tienen caen juntos en «Información del municipio».
Quedan cuatro pestañas y dieciséis apartados.

**Normativa municipal queda fuera a propósito.** Es el único epígrafe del diseño
que ya está construido en otro sitio: la biblioteca de ordenanzas con su búsqueda
semántica, más el área fija de Normativa. Sembrarlo aquí habría bifurcado el
dominio, que es justo lo que la fase B6 decidió no hacer.

**Se siembra bajo petición, no al arrancar**, por la misma razón que ADR-043:
hacerlo en el `lifespan` impondría la estructura a organizaciones que no la
quieren y resucitaría en cada reinicio lo que alguien archivó a conciencia.

**Y no siembra contenido municipal.** Ni un teléfono, ni un concejal, ni un dato
del padrón: eso sólo lo tiene el ayuntamiento, y un dato de ejemplo en una
pantalla institucional es peor que una pantalla vacía. El seed crea el esqueleto;
lo que va dentro lo escribe quien responde de ello.

**La idempotencia aguanta el renombrado.** Cada bloque sembrado lleva su clave en
`data_json` junto al formato, así que el ayuntamiento que llame «Teléfonos» a
«Teléfonos de interés» no se encuentra un duplicado en la siguiente llamada. Si
no hay marca se compara el título, que cubre el caso contrario: la pestaña la
creó alguien a mano antes de sembrar y lo que falta son sus apartados. Archivado
sigue archivado. Hay tests para los tres casos.

La marca convive con `write_layout` de las rutas: ambos conservan lo que ya
hubiera en `data_json`, de modo que cambiar el formato de un apartado sembrado no
borra su clave ni al revés.

## ADR-054: El Ayuntamiento recupera el cuarto nivel del diseño (2026-09-01)

ADR-053 colapsó el nivel del epígrafe: el diseño tiene pestaña → epígrafe →
pestaña interna → contenido y el modelo tenía pestaña → apartado → elemento, así
que cada epígrafe con pestañas internas se convirtió en pestaña hermana. La
consecuencia se vio al comparar capturas del export completo con la aplicación:
donde el diseño enseña **una** pestaña «Información» con seis tarjetas plegables,
la aplicación enseñaba **cuatro** pestañas hermanas. Anthony pidió ceñirse al
diseño, así que se revierte el colapso.

**El modelo pasa a cuatro niveles**: `nav_section` (pestaña) → `epigraph`
(la tarjeta plegable) → `nav_item` (su pestaña interna, el apartado) → `item`
(el contenido). No hace falta tocar la restricción de la tabla: el check de
`block_type` ya admitía `epigraph` desde ADR-034. La migración
`20260901_0043` da a cada pestaña existente un epígrafe que hereda su título y
adopta sus apartados, y el `downgrade` los devuelve a la pestaña y renumera.

**El formato sigue siendo del apartado, no del epígrafe.** Era la razón técnica
del colapso y sigue en pie: la demografía es una serie, el análisis de agua son
ficheros y los teléfonos son contactos. Con el nivel recuperado deja de ser un
problema, porque el epígrafe ya no tiene que elegir uno: cada una de sus pestañas
internas trae el suyo. La tarjeta enseña el apartado abierto y la fila de
pestañas internas aparece sólo cuando hay más de uno.

**Sólo se siembra la pestaña «Información».** El diseño tiene cuatro
—Información, Administración, Personal y Mapa general— pero las otras tres ya
tienen módulos propios en la aplicación, y sembrarlas como bloques vacíos
bifurcaría el dominio: el mismo motivo por el que ADR-053 dejó fuera Normativa.
Quedan una pestaña, seis epígrafes y dieciocho apartados, en el orden de
`infoDefault`.

La fila de pestañas del Ayuntamiento sigue llevando las áreas fijas de ADR-052
por delante de las configurables. Reducirla a las cuatro del diseño es una
decisión aparte, todavía sin tomar: obliga a redirigir los enlaces `?tab=` que
hay repartidos por el producto y a decidir dónde va la ficha de identidad
municipal, que el diseño no contempla.

## ADR-055: El proxy WMS de SIUR se encuentra con el GeoServer real (2026-09-01)

La entrega por proxy de las capas de SIUR estaba implementada por completo
—catálogo, cadena de evidencia, teselas, leyenda e identificación— pero nunca se
había ejecutado contra `idecyl.jcyl.es`. Sus pruebas usan documentos sintéticos
y, al contrastarla con el servicio real, ninguna capa podía llegar a servirse.
Aparecieron cuatro obstáculos, todos en el camino que va del GetCapabilities
descargado a la atestación.

**El importador no arrancaba.** `siur_delivery_import` era el único CLI del
módulo que no llamaba a `register_all_models()`, así que SQLAlchemy no podía
resolver las relaciones por nombre y el mandato moría con «No se pudo completar
la transacción» antes de leer nada. Se alinea con `siur_sync` y
`mirror_review_packet`.

**GeoServer anuncia su endpoint con el servicio ya seleccionado.** Todos los
espacios de trabajo de IDECyL publican su `OnlineResource` como
`/geoserver/<ws>/ows?SERVICE=WMS&`, y el parser rechazaba cualquier cadena de
consulta. Ese prefijo no lleva parámetros de petición, así que ahora se descarta
en lugar de rechazarse; cualquier otro parámetro —un token, un `bbox`, otro
servicio— sigue siendo fatal. La comprobación vive en `_require_service_selector_only`
y `validate_siur_wms_endpoint` no se toca: sigue prohibiendo cadenas de consulta
en todo lo demás, incluida la URL que el proxy acaba llamando.

**`/ows` y `/wms` son el mismo servlet.** El catálogo guarda la forma `/wms` que
viene de `settings.json` y GetCapabilities declara la forma `/ows`. Atar la
atestación a su servicio con una igualdad literal de cadenas las hacía
incompatibles siempre. `siur_wms_endpoints_are_equivalent` compara el espacio de
trabajo dentro del allowlist, de modo que la atadura sigue existiendo pero deja
de depender de la grafía.

**La versión declarada del catálogo se respeta cuando existe.** SIUR sólo declara
versión WMS en 3 de sus 33 servicios y deja el resto a nulo. Un nulo significa
«el catálogo no lo sabe», no «vale cualquier versión»: cuando hay versión
declarada tiene que coincidir con la atestada, y cuando no la hay la
instantánea de capacidades —inmutable y con hash— es la autoridad. No se relaja
nada más, porque la propiedad de que la evidencia desajustada corta la entrega
antes de tocar la red es justamente lo que protege este módulo.

**Lo que sigue sin poder servirse.** IDECyL no anuncia `GetLegendGraphic` ni
`application/json` en `GetFeatureInfo`, así que leyendas e identificación
permanecen no disponibles por diseño del origen, no por una limitación nuestra.
Los tres servicios con versión `1.1.1` declarada (`urbanismo`, `limites`,
`entidades`, 28 capas) tampoco pueden atestarse todavía: su GetCapabilities
1.1.1 lleva DOCTYPE y el parser prohíbe DTD por defensa XXE, y el 1.3.0 que sí
se puede leer contradice la versión del catálogo. Desbloquearlos exige una
promoción de catálogo revisada, no una relajación de la comprobación.

**Añadido el 2026-09-01, al preparar la promoción de producción.** El WMC que
`settings.json` declara hoy (`assets/wmcs/default.xml`) trae la capa
`plau_cyl_planes_parciales` con **dos** estilos marcados `current="1"`. El parser
rechazaba el documento entero por ambiguo, lo que dejaba la promoción sin sonda
posible: el WMC revisado en julio ya no sirve porque SIUR ha partido
`ot_cyl_instrumentos_ambito` en `_regional` y `_subregional`, y su identidad
antigua no casa. Ahora una selección ambigua **no selecciona nada** en esa capa
en lugar de tumbar el documento: la capa conserva el estilo predeterminado que
`settings.json` ya deriva y la sonda simplemente deja de confirmar un
predeterminado ahí. No se adivina, y el resto del WMC sigue comprobándose.

## ADR-057: El espejo cartográfico se acota al municipio servido (2026-09-03)

Los perfiles de cobertura del espejo local fijaban la envolvente de Castilla y
León entera con zoom nativo 16. Al medirlo contra el servicio real del IGN por
primera vez, ese alcance resultó ser 1.308.502 teselas, **28,0 GB y 20.627
peticiones** para una sola capa de fondo: casi seis horas de tráfico contra un
servicio público, repetidas en cada refresco, para servir a un municipio de
Burgos. Y no cabía: la caché revisada del despliegue son 32 GiB con una cuota de
24, así que la pirámide regional no llegaba a poder almacenarse.

**El perfil pasa a ser municipal**: el municipio servido más un anillo de
vecinos, unos 30 × 31 km, con zoom nativo 17. Son **24.509 teselas, unos 400 MB
y 438 peticiones**, y llega **un nivel de zoom más cerca** que el perfil
regional: el ayuntamiento ve sus parcelas en lugar de una región borrosa. Cuesta
la setentava parte y da más detalle.

Las cifras no son estimaciones. Salen de descargar supertiles reales de
`wms-inspire/ign-base` replicando las peticiones que emite `tile_seed`, y de
medir tamaño y latencia por nivel de zoom: 16,7 KB por tesela en z16 frente a
55,8 KB en z14, y entre 3,4 y 9,4 segundos por petición según el nivel. Una
estimación anterior basada en teselas WMTS dio 20 GB y se quedó corta, porque el
WMTS del IGN sirve PNG pre-renderizados mucho más ligeros que los que devuelve
su WMS al vuelo. Medir el camino que el código recorre de verdad, y no uno
parecido, fue lo que cambió la decisión.

**El sobre vale para todo lo que se adquiere de SIUR**, no sólo teselas: las
láminas de inundación de MITECO se descargaban por API de features con el mismo
`bbox` regional y ahora se acotan igual. La desproporción era la misma.

**La sustitución de ortofotos históricas conserva su perfil regional.** Su
evidencia de equivalencia está comprometida en el repositorio y verificada byte
a byte, de modo que su perfil operativo no puede moverse sin regenerar ese
fichero y su hash. Es una funcionalidad distinta de los fondos que este
despliegue replica, así que se le da su propia constante de límites y se deja
intacta.

**Servir un segundo municipio exige revisar un segundo perfil.** No se deriva el
sobre de los datos del municipio en tiempo de ejecución a propósito: la
cobertura entra en la definición de cada fuente y en su hash, y una cobertura
que cambia sola invalidaría en silencio las autorizaciones de espejo que
dependen de ella.

## ADR-058: El catálogo verifica la instantánea una vez por lectura, no una por capa (2026-09-03)

**Contexto.** El mapa de producción aparecía vacío con dos mensajes:
«Cargando catálogo verificado…» en el árbol de capas y «Fondo cartográfico local
pendiente de sincronización» sobre el lienzo. No era un fallo de entrega: la capa
de fondo estaba sembrada, autorizada y servía teselas en todos los niveles de
zoom. Era el catálogo, que **tardaba unos ocho segundos y medio** en responder.
Durante ese rato la pantalla es exactamente la de un mapa roto.

Medido con perfilador contra la base de datos de producción, el coste estaba en
dos sitios, y ninguno era el que parecía:

1. `_current_layer_blocker` llamaba, **por cada una de las 228 capas**, a
   `stored_catalog_snapshot_is_valid` y a `catalog_snapshot_contains_active_layer`.
   Las dos revalidan la instantánea entera: 460 serializaciones y hashes del
   mismo documento inmutable, 2,2 s sólo en `json.dumps`.
2. La disponibilidad local pedía la cadena de autorización **una consulta por
   fuente**: 536 viajes a la base de datos.

**Decisión.**

1. **`CatalogSnapshotDeliveryView`**: la instantánea se valida **una vez por
   lectura de catálogo** y se indexan de una pasada las `source_key` que quedaron
   entregables. Cada capa consulta ese índice. La instantánea es inmutable y no
   puede cambiar mientras se la lee, así que la respuesta no puede diferir entre
   una capa y la siguiente. **La verificación no se debilita: se deja de repetir.**
2. **`prefetch_source_authorization_chains`**: las cadenas de autorización de
   todas las fuentes se cargan en una consulta y se le pasan al mismo
   `require_current_source_authorization` de siempre. Cambia de dónde salen las
   filas, no qué se comprueba con ellas.
3. **Los caminos de una sola capa se quedan como estaban.** La vista y la
   precarga son parámetros opcionales que sólo usa el camino masivo del catálogo,
   de modo que la entrega de una tesela concreta sigue revalidando por su cuenta.

**Resultado**, medido contra producción: la proyección pasa de **8,4 s a 0,29 s**
y de 552 consultas a 26, con salida idéntica —las mismas capas entregables, los
mismos motivos de bloqueo y la misma atribución proyectada.

**Consecuencias.** Una prueba fija el invariante que hace legítimo el atajo: la
vista tiene que responder lo mismo que la comprobación por capa para todas las
capas de una instantánea, y fallar cerrada cuando la instantánea está corrupta.
Si alguna vez la validación pasa a depender de algo que cambie dentro de una
misma petición, esa prueba es la que se romperá, y con razón.

## ADR-059: La fila de pestañas del Ayuntamiento la forman los apartados (2026-09-03)

**Contexto.** El diseño abre el Ayuntamiento con cuatro pestañas —Información,
Administración, Personal y Mapa general— y el asa que precede a la fila ofrece
*Renombrar · Añadir apartado · Eliminar apartado*. Es decir: en el diseño una
pestaña se llama «apartado» y la fila **es dato configurable**, no un conjunto
de áreas fijas del producto.

La aplicación lo tenía al revés. Había seis áreas fijas —Información, Normativa,
Servicios municipales, Mapa general, Personal, Hoja de ruta— y los apartados
configurados se añadían **detrás**. Una organización sembrada enseñaba diez
pestañas y la fila se partía en dos líneas; además «Información» salía dos
veces, el área fija y el apartado que el seed acababa de crear.

**Decisión.**

1. **La fila la forman los apartados.** El primero ocupa el sitio de
   «Información» —conserva el identificador `summary` para no romper los enlaces
   `?tab=summary`— y lleva su propio título y sus epígrafes. Los demás mantienen
   pestaña propia, que es la configurabilidad que el diseño tiene. Detrás van
   Administración, Personal y Mapa general.
2. **Normativa y Hoja de ruta salen de la fila.** La aplicación ya las sirve en
   `/ordenanzas` y `/hoja-de-ruta`, y el diseño las trata como pantallas propias.
   Sus enlaces `?tab=` siguen resolviendo para poder redirigir a la ruta en vez
   de dejar al visitante en una pestaña que ya no existe.
3. **Servicios municipales pasa a Administración**, que es donde el diseño la
   pone: no es hermana de Información, es uno de sus seis epígrafes.
4. **Un epígrafe puede declarar un módulo** que el producto ya sabe pintar. El
   enlace viaja en la clave del seed guardada en `data_json`, **no en el
   título**, para que renombrar la tarjeta no la desconecte. Hoy sólo la
   corporación municipal reclama uno.
5. **Las pestañas sobrantes del seed de ADR-053 se pliegan** dentro de
   «informacion» (migración `20260903_0044`): sus epígrafes pasan a colgar de
   ella y la pestaña vacía se archiva. Sólo se tocan las que conservan su marca
   del seed; una pestaña creada a mano no la lleva y se queda donde está. Cada
   epígrafe movido anota su origen, de modo que la vuelta atrás no depende de
   títulos que el ayuntamiento puede haber cambiado.

**Consecuencias.** La fila de una organización sembrada de nuevo es exactamente
la del diseño. El masthead del Ayuntamiento desaparece y el escudo se muda a la
barra superior, que ya sabía pintarlo pero nunca lo recibía. Quedan fuera, y
anotadas: los contenidos a medida de cada epígrafe —las gráficas de doble eje,
el carrusel de analíticas, la regla de visitas del arquitecto— que el inventario
de interacción documenta y que no son bloques genéricos, sino componentes por
construir uno a uno.

## ADR-060: El mapa deja de ahogarse a sí mismo al moverlo (2026-09-03)

**Contexto.** Con el fondo ya servido y el catálogo rápido (ADR-058), el mapa
seguía siendo incómodo de usar: cargaba a trompicones y se atascaba al
desplazarlo. Medido contra producción, había tres causas distintas, y ninguna
era la entrega de la tesela.

1. **Cada tesela costaba 106 ms de CPU.** Para servir un cuadrado de 256 píxeles
   se revalidaba la instantánea del catálogo seis veces y **la evidencia de las
   228 filas de la matriz de estrategias**, unas 250 serializaciones JSON. El
   backend corre con **un único worker** (ADR-010/015), así que veinte teselas
   son dos segundos de cálculo en cola por los que espera todo lo demás.
2. **Cada tesela fallida pedía el catálogo entero.** El espejo cubre un sobre
   finito y salirse de él devuelve 404 **por diseño**; acercarse al borde
   generaba decenas de 404 y, con ellos, decenas de recargas del catálogo. La
   aplicación se ahogaba justo cuando más se la movía.
3. **«Sin fondo» se guardaba como decisión del usuario** aunque no hubiera
   ningún fondo que elegir. Todo navegador que abriera el mapa antes de sembrar
   el espejo se quedaba sin fondo **para siempre**, y no había forma de que se
   recuperase solo. Le pasó al primer usuario real.

**Decisión.**

1. **La instantánea se valida una vez por resolución de entrega**, con el mismo
   `CatalogSnapshotDeliveryView` de ADR-058 memorizado por identificador dentro
   de la llamada. Sin concesión alguna: 106 → 76 ms.
2. **La generación de estrategias se verifica entera y ese veredicto se confía
   15 segundos.** La huella se calcula sobre las filas ya cargadas —id, capa,
   generación, `evidence_sha256`, hash de definición—, así que una
   reconciliación se detecta en la petición siguiente. Lo que la ventana aplaza
   es sólo lo que la huella no puede ver: evidencia editada en el sitio dejando
   intacto su hash. **Es un límite explícito y de un solo número**, en lugar de
   un coste pagado en cada tesela: 76 → 51 ms.
3. **Un fallo de tesela refresca el catálogo como mucho cada 30 segundos.** El
   aviso al usuario se mantiene; lo que se corta es la avalancha.
4. **La preferencia de mapa base distingue «no elegí» de «elegí ninguno».** Se
   guarda en `baseLayerChoice`, y sólo se anota una decisión cuando había algún
   fondo que elegir. El `baseLayerId: null` del formato antiguo se lee como «sin
   elegir», porque no se puede distinguir de la carencia que lo escribía y esa
   carencia la vivieron todos los navegadores anteriores a la siembra. A quien
   de verdad quisiera «Sin fondo» le cuesta un clic volver a decirlo; a quien lo
   tuviera por el fallo, el mapa le vuelve solo.

**Consecuencias.** Quedan 51 ms y 14 consultas por tesela, ahora dominados por
viajes a la base de datos y no por revalidación. Bajar de ahí exigiría cachear
la resolución de entrega completa, con su propia pregunta de caducidad, y no se
hace hasta que se demuestre necesario. El único punto donde este ADR cede
frescura —el segundo— tiene su constante a la vista y una función de reinicio
para las pruebas, de modo que la próxima persona vea el precio antes de tocarlo.

## ADR-061: El mapa se abre y se detiene donde llega su cartografía (2026-09-03)

**Contexto.** Dos quejas del primer usuario real, y las dos con la misma raíz.
El zoom permitía acercarse hasta el nivel 24 cuando el archivo local sólo tiene
hasta el 17: los últimos siete niveles no añadían detalle, sólo agrandaban la
misma tesela hasta que el rótulo del pueblo llenaba la pantalla. Y al entrar en
la sección el mapa no se abría sobre el municipio, sino donde dijera una
constante escrita en el código.

Esa constante era mía y siempre fue un apaño: unas coordenadas de Fuentelcésped
puestas a mano porque el visor abría en la capital de provincia. Servía para un
despliegue y sólo para uno.

**El dato existía y no llegaba.** `settings.json` de SIUR no publica límites ni
zooms para sus mapas de fondo, así que el catálogo los guarda a nulo y el visor
se quedaba sin nada con lo que encuadrarse ni con lo que frenar. Pero el espejo
**sí lo sabe**: la envolvente y el zoom nativo forman parte de la definición
revisada de la fuente y entran en su hash (ADR-057). `Municipality`, en cambio,
no tiene coordenadas: no hay de dónde sacar el centro del pueblo por esa vía.

**Decisión.**

1. **El catálogo proyecta la cobertura que el espejo sirve de verdad**
   (`catalog_served_tile_coverage`): límites y zooms de la fuente revisada, sólo
   para capas con entrega local activa, y **sólo donde el catálogo no declara ya
   un valor propio**. No muta nada, igual que la proyección de atribuciones.
2. **El visor se abre encuadrado en esa envolvente** cuando no hay elementos que
   situar ni un punto enfocado. Ya no hay coordenadas de ningún municipio
   concreto en el código: un segundo ayuntamiento se abre sobre su pueblo porque
   su perfil de cobertura es otro, no porque alguien edite una constante.
3. **El zoom se detiene dos niveles por encima del nativo.** Se usa
   `maxNativeZoom` para que Leaflet amplíe la última tesela archivada, y el mapa
   se limita a `nativo + 2`. Dos niveles amplían de forma útil; el resto era
   ruido. Sin cobertura declarada se conserva el tope de siempre.
4. **Los límites llegan también a la capa de teselas**, así que Leaflet deja de
   pedir cuadrados fuera de la zona replicada en lugar de coleccionar 404.

**Consecuencias.** La constante de reserva sigue existiendo para el instante
anterior a que el catálogo cargue, pero deja de decidir nada en cuanto llega el
fondo. Al alejar el zoom se sigue viendo la envolvente municipal recortada sobre
el vacío: eso es ADR-057 y no se arregla aquí; se arreglaría sembrando los
niveles lejanos de un área mayor, que es barato en teselas pero obliga a rehacer
las autorizaciones y a resembrar.

## ADR-062: Una tesela que falla no tira la siembra entera (2026-09-03)

**Contexto.** Sembrar el espejo son decenas de miles de peticiones seguidas
contra un servicio público. `tile_seed` no tenía **ningún** reintento: la
primera respuesta mala abortaba la ejecución completa. Con la ortofoto del IGN,
que es mucho más pesada de renderizar y devuelve 502 bajo carga, eso convertía
cualquier intento en una lotería; y para pirámides de horas significa tirar el
trabajo hecho y el ancho de banda ajeno ya consumido.

Lo llamativo es que **la clasificación ya existía y no se usaba**:
`SafeDownloadError` lleva `retryable` desde su diseño, `RETRYABLE_HTTP_STATUSES`
incluye 408, 425, 429, 500, 502, 503 y 504, y la capa de descarga ya leía la
cabecera `Retry-After`. Lo único que faltaba era hacerle caso.

**Decisión.** `retrying_tile_fetcher` envuelve el descargador real y reintenta
**sólo lo que la capa de descarga clasificó como transitorio**:

- **Un 403, un 404, una URL fuera del origen revisado o un tipo de contenido
  incoherente siguen fallando a la primera.** Eso es lo que significa fallar
  cerrado, y reintentarlo sería insistirle a un servicio que ya ha dicho que no.
- La espera crece de forma exponencial desde `REFERENCE_TILE_RETRY_BASE_SECONDS`
  con techo en `REFERENCE_TILE_RETRY_MAX_SECONDS`, y lleva **dispersión**: con
  varias hebras a la vez, esperar todas exactamente lo mismo vuelve a golpear el
  origen en bloque.
- Un `Retry-After` del origen **manda** sobre nuestra espera, pero **sin pasar
  del techo**: se respeta que pida más tiempo, no que nos pare diez minutos.
- Agotados los intentos, **se propaga el error original**, no uno genérico: si
  el origen lleva media hora devolviendo 502, eso es lo que hay que poder leer.

**Consecuencias.** El tope por defecto —cuatro intentos, 30 segundos de espera
máxima— es deliberadamente modesto: un origen realmente caído no debe convertir
una siembra de siete minutos en uno de esos procesos que nadie sabe si sigue
vivo. Los reintentos se registran con su código y su número de intento, así que
un origen degradado se ve en el registro en vez de esconderse detrás de una
siembra lenta.

**Lo que esto NO arregla:** no hay reanudación. Si se agotan los intentos, la
ejecución sigue perdiéndose entera en lugar de continuar por donde iba. Eso es
trabajo aparte y más caro, porque exige persistir el avance parcial sin romper
la garantía de que un archivo publicado está completo y verificado.

## ADR-063: Un valor admitido repetido no invalida un servicio (2026-09-03)

**Contexto.** La ortofoto (PNOA) llevaba semanas fuera del espejo y su fuente
primaria se rechazaba **siempre** con `invalid_capabilities`, sin más
explicación: el error real de la sonda se envolvía y se perdía. Reproducida la
sonda sobre el documento auténtico, el motivo resultó ser
`capabilities contains duplicate values`.

La causa: **el IGN declara `EPSG:32631` dos veces** entre los veintiún sistemas
de referencia de su capa raíz. Por esa redundancia, `_direct_child_texts`
rechazaba el documento entero y la ortofoto quedaba inalcanzable.

Lo decisivo es que la comprobación era **incoherente con el propio código**: la
línea siguiente ya combinaba esos valores con `dict.fromkeys`, es decir, ya los
trataba como un conjunto. Se exigía que no se repitieran para acto seguido
deduplicarlos.

**Decisión.** `_direct_child_texts` deduplica conservando el orden. Sólo se usa
para leer listas de «lo que este servicio admite» —formatos de imagen y sistemas
de referencia—, donde declarar dos veces un valor no dice nada distinto de
declararlo una vez.

**La duplicación que sí importa se sigue rechazando**, y en su sitio: dos
colecciones con el mismo nombre hacen ambiguo a qué capa se refiere uno, y eso
falla cerrado en `duplicate collections`, que es una comprobación aparte.

**Consecuencias.** Con esto la sonda del PNOA pasa: capa `OI.OrthoimageCoverage`
disponible, versión 1.3.0. Queda el segundo obstáculo de la ortofoto, que es
otro y no se arregla aquí: el IGN devuelve 502 bajo carga al renderizarla, para
lo cual están los reintentos de ADR-062.

**Lección de método**, porque costó semanas: envolver una excepción sin dejar
rastro del motivo original convierte un fallo diagnosticable en uno opaco. Es el
mismo patrón que ya se corrigió en `unexpected_worker_error`. Al escribir un
`raise ... from error`, conviene preguntarse si alguien podrá saber qué pasó sin
volver a reproducirlo a mano.
