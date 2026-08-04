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

## ADR-034: Barra superior municipal fija y tipografía institucional (2026-08-03)

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
## ADR-035: Dominios tenant-scoped de gobierno y personal (2026-08-03)

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
