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

## ADR-030: Barra municipal configurable sobre bloques de contenido (2026-07-29)

La pantalla «Ayuntamiento» del diseño (Claude Design, `Pantalla Principal.dc.html`) deja al usuario crear, renombrar, reordenar y borrar libremente los apartados de su municipio, además de poner su escudo, su nombre y la temperatura del día. Se implementa **extendiendo** el `MunicipalWorkspace` existente, no sustituyéndolo: aquél agrega los módulos operativos ya construidos (inventario, mantenimiento, normativa, personal) y esta decisión le añade el cromo editable.

El contenido se modela como **bloques genéricos** (`municipal_blocks`: padre, posición, título, cuerpo y carga libre en JSON) en lugar de tablas tipadas por dominio. Tablas tipadas serían más consultables y validables, pero obligarían a una migración por cada epígrafe nuevo y no admiten el «añade el apartado que quieras» que el diseño da por supuesto. El check de `block_type` ya admite los tipos reservados de contenido (`epigraph`, `section`, `item`) para que la fase siguiente no necesite migración; la API solo crea los dos de navegación. Los apartados creados aparecen como pestañas tras las áreas fijas, de modo que la pantalla conserva una sola navegación.

El contenido cuelga de `Organization`, no de `Municipality`. `Municipality` es dato de referencia **global compartido entre inquilinos**, mientras que la ficha del Ayuntamiento la edita cada inquilino para sí: colgarla del municipio filtraría contenido editable entre organizaciones. El perfil (`municipal_profiles`) guarda nombre mostrado, escudo, interruptor y coordenadas del bloque de temperatura, uno por organización. La organización la indica siempre quien consume la API: el espacio municipal ya sabe cuál está seleccionada, y un superusuario no tiene «primera organización».

El escudo **no** usa el modelo `Document`: `documents.project_id` es `NOT NULL` y su control de acceso es el del proyecto, ninguna de las dos cosas encaja con una imagen de marca de la organización. Reutiliza el servicio de almacenamiento bajo el prefijo `organizations/<id>/brand/`, conservando escritura por trozos, checksum y defensa de path traversal, con lista blanca propia (solo imágenes) y tope propio (2 MiB).

El bloque de temperatura usa **Open-Meteo** consultado **desde el backend** (`app/town_hall/weather.py`), nunca desde el navegador: así el ayuntamiento no expone a sus usuarios a un tercero. Es un punto de egreso externo nuevo, el primero que no es de IA, y por eso queda fuera del gateway (ADR-013), de `speech.py` (ADR-021) y de `web_search.py` (ADR-022), pero se somete a su misma disciplina: por él solo salen el topónimo público —una sola vez, para geocodificarlo, sin el sufijo de provincia, que el buscador no entiende— y sus coordenadas, jamás datos de usuarios, documentos ni conversaciones, y los logs registran solo metadatos. Open-Meteo se elige sobre AEMET por no requerir clave de API ni alta; el dato es orientativo, no oficial. Las coordenadas resueltas se guardan y se invalidan al cambiar la localidad. La temperatura se cachea en memoria 30 minutos, con la misma condición que el limitador de login: mover a Redis antes de ir a multi-worker (ADR-010). Se pide en un endpoint aparte de la pantalla, de modo que una caída del proveedor no impida cargarla: el bloque muestra entonces un guion, nunca una cifra inventada.

## ADR-031: Publicación en dominio público con proxy inverso único (2026-07-30)

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

## ADR-032: Endurecimiento del backend expuesto a Internet (2026-07-30)

Antes de este cambio el backend tenía **un solo middleware** (CORS), publicaba `/docs` y `/openapi.json` sin autenticar, no emitía ninguna cabecera de seguridad, no tenía más defensa CSRF que `SameSite=Lax`, no configuraba logging —de modo que todo `logger.info`, incluida la telemetría del gateway de IA, se descartaba en silencio— y no registraba ni un solo evento de seguridad. Nada de eso era grave en `localhost` y todo lo es en un dominio público.

**Guardas de arranque.** Con `ENVIRONMENT=production` el backend se niega a arrancar si `SECRET_KEY` es uno de los valores de ejemplo documentados o mide menos de 32 caracteres, si algún origen de CORS no es `https://` o es `*`, o si `ALLOWED_HOSTS` es `*`. El motivo es concreto: el `.env` de desarrollo se copia con facilidad y llevaba `SECRET_KEY=change-me-...`, `BOOTSTRAP_ADMIN_TOKEN=dev-bootstrap-token` y orígenes en claro; el fallo tenía que ser ruidoso y no silencioso. `environment` se valida además contra un enum **sin normalizar**: aceptar `Development` como equivalente de `development` habría ablandado la puerta de ADR-024, que exige la cadena exacta para el puente Codex.

**Superficie.** `/docs`, `/redoc` y `/openapi.json` solo existen en desarrollo: describían las ~132 rutas, sus esquemas y la cabecera del token de bootstrap sin pedir credenciales. Se añade `TrustedHostMiddleware` con los hostnames reales.

**CSRF.** Se añade una comprobación de `Origin` en los métodos de escritura, como segunda capa sobre la cookie. Si llega `Origin` y no es de los nuestros, 403; si no llega, se permite, porque los clientes de API con `Authorization: Bearer`, el webhook de Telegram y el `TestClient` no lo envían mientras un navegador sí lo hace en toda escritura. No es un token sincronizador —seguiría siendo lo más sólido— pero cubre la forma real del ataque sin romper los caminos legítimos existentes.

Los middlewares se escriben como **ASGI puro** y no sobre `BaseHTTPMiddleware`: el asistente responde por SSE y `BaseHTTPMiddleware` se interpone en el streaming.

**CSP.** La política del frontend se sirve desde el proxy y está verificada contra el código: `img-src` admite las teselas de OpenStreetMap que carga Leaflet, `font-src 'self'` basta porque `next/font` sirve IBM Plex desde nuestro host, y el micrófono se permite porque el modo voz lo usa. `script-src` necesita `'unsafe-inline'`: Next.js App Router inyecta scripts inline para hidratar y el script de tema también es inline. Apretarlo a CSP con nonce exige un `frontend/middleware.ts` y **queda como deuda declarada**, no como algo resuelto. Las respuestas de `/api/*` llevan en cambio `default-src 'none'; sandbox`, que neutraliza cualquier fichero servido con un tipo ejecutable.

**Cuatro vulnerabilidades corregidas.** (1) `GET /town-hall/shield` servía el escudo **inline**, sin `Content-Disposition`, con el content-type declarado por el cliente y `image/svg+xml` en la lista blanca: un usuario con `town_hall.edit` podía almacenar un SVG con script que se ejecutaba en el origen de la API con la cookie de sesión en alcance, y al ser una navegación `GET` la cookie `Lax` viajaba igual. Se sirve como adjunto y la extensión de la cabecera se deriva del content-type validado, no del nombre original, que es el que elige quien sube. Se conserva el soporte de SVG porque el frontend lo pinta en un `<img>`, donde no puede ejecutar scripts. (2) El webhook de Telegram **fallaba en abierto**: sin secreto configurado se saltaba la verificación entera. (3) La llamada gRPC a NVIDIA Riva y el subproceso de ffmpeg no tenían límite temporal y podían inmovilizar el único worker. (4) Hermes, embeddings y Azure usaban `urlopen` desnudo, que sigue redirecciones **reenviando la credencial**; el patrón que ya existía para OpenAI se generaliza a un ayudante compartido.

**Límites de tasa.** Solo login y cambio de contraseña estaban limitados; quedaban abiertos los endpoints que cuestan dinero o CPU en cada llamada: turnos de LLM, síntesis de voz, transcripción (subida de 20 MiB más un ffmpeg por petición), subidas e importación de ordenanzas. Se extienden reutilizando el limitador en memoria en lugar de migrar a Redis: con dos usuarios y un solo worker, mover el estado añade un modo de fallo sin beneficio, y la condición de ADR-010/ADR-015 (Redis antes de multi-worker) sigue en pie. Se añade un **techo por IP** en el login: la clave anterior incluía la cuenta, así que una sola IP podía probar una contraseña contra cuentas distintas sin límite alguno.

**Traza de auditoría.** `security_events` es inmutable por trigger en la base, con el mismo patrón que `maintenance_order_events`, y cubre `UPDATE` **y** `DELETE`: una traza que la propia aplicación pueda reescribir no sirve como prueba, y un backend comprometido no debe poder borrar su rastro. Se instrumentan una decena de puntos de alto valor (login correcto, fallido y bloqueado, logout, cambio y reseteo de contraseña, creación del admin de bootstrap, alta y baja de superusuario, borrado de usuario, subida, descarga y archivado de documentos) en lugar de las 132 rutas. El acceso a documentos es lo que hace defendible el piloto con datos reales. `detail` guarda solo metadatos, nunca contenido: la regla dura del proyecto se mantiene. Se lee por un endpoint **solo de superusuario** y fuera del catálogo RBAC, porque la traza cruza organizaciones —un login fallido no tiene organización— y porque así ningún rol municipal puede concederse acceso a sí mismo. Guarda IP y correo intentado, que son datos personales: la retención es de 90 días con purga programada.

## ADR-033: Copias de seguridad en el servidor con snapshots del proveedor (2026-07-30)

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
