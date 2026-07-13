# Arquitectura

Actualizado: 2026-07-13.

Este documento describe la arquitectura **implementada**. No representa por sí
solo la arquitectura objetivo. La decisión de producto es una instancia por
ayuntamiento con control de plataforma separado; véanse
`docs/vision-producto.md` y `docs/analisis-repositorio-2026-07-13.md`.

## Visión general

Aplicación multi-tenant actual con cinco servicios en Docker Compose:

- `backend`: API HTTP FastAPI (puerto 127.0.0.1:8000), monolito modular.
- `worker`: consumidor RQ de trabajos encolados por ordenanzas y oficina de agentes.
- `frontend`: Next.js App Router (puerto 127.0.0.1:3000), consola de administración y trabajo.
- `postgres`: PostgreSQL 17, interno (sin puerto publicado), con volumen persistente.
- `redis`: Redis 7, interno, usado como cola de trabajos RQ.

## Modelo de dominio y tenancy

La distinción central del dominio:

- `Organization` es el **tenant**: la entidad cliente que usa la aplicación. Delimita usuarios (vía membresía), grupos, proyectos, documentos y requisitos.
- `Municipality` es **dato de referencia global**: municipios reales de España, compartidos entre tenants, base de la futura base de conocimiento comparativa. Una organización puede enlazar opcionalmente con un municipio.
- `Project` (expediente/área de trabajo) y `Document` viven dentro de una organización. `Requirement` pertenece a una organización y opcionalmente a un proyecto.
- `GeoLocation` y `EntityLocation` forman la capa geográfica compartida del mapa municipal: las ubicaciones pertenecen opcionalmente a una organización/municipio y se vinculan a entidades como necesidades o proyectos sin duplicar columnas `lat/lng` en cada módulo.
- `Ordinance` es global, pertenece a un municipio y puede enlazar a un documento de un tenant; ese enlace exige que quien lo crea tenga acceso al documento.

## Control de acceso

- Autenticación: JWT HS256 de acceso (60 min, con `iat`) entregado en cookie httpOnly SameSite=Lax al navegador (`POST /auth/logout` la limpia y exige sesión); la cabecera Bearer sigue aceptada para API/tests. Contraseñas con Argon2id, nunca recortadas; cambio self-service (`POST /auth/change-password`, reemite la cookie) y reset por administradores (con guarda: solo superusuarios resetean a superusuarios); ambos revocan los tokens emitidos antes (`iat` vs `users.password_changed_at`, ADR-015). Rate limiting en memoria por cliente+cuenta en login y cambio de contraseña: solo los intentos fallidos consumen cupo.
- Autorización: cadena RBAC usuario → grupo → rol → permiso. Los permisos de un grupo solo cuentan si el usuario es además miembro de la organización del grupo, lo que hace el modelo consciente del tenant.
- `is_superuser` puentea todos los chequeos. Conceder o retirar superusuario es operación de superusuarios.
- Operaciones globales reservadas a superusuarios: crear/editar/borrar roles y permisos, asignar permisos a roles, crear organizaciones (tenants).
- `users.manage` está delimitado por organización: un administrador solo gestiona usuarios que comparten alguna organización donde él tiene el permiso.
- Municipios y ordenanzas son globales: sus permisos (`municipalities.*`, `ordinances.*`) se evalúan sin filtro de organización; quién debe curarlos es una decisión de producto abierta.
- El mapa municipal añade permisos propios (`map.view`, `map.edit`, `map.import`, `map.manage`). Los marcadores combinan permiso de mapa en la organización de la entidad con la visibilidad normal de la necesidad/proyecto, para que la capa geográfica no filtre trabajo inaccesible por otra ruta.
- El catálogo de permisos se siembra automáticamente al arrancar el backend (idempotente); `POST /admin/permissions/bootstrap` sigue disponible como re-siembra manual. El arranque también siembra fuentes jurídicas oficiales mínimas para importación de ordenanzas, incluido el BOP de Burgos como fuente primaria del MVP Burgos.

## Documentos

- Los bytes se guardan en el sistema de archivos (`DOCUMENT_STORAGE_ROOT`, volumen Docker `document_storage`); PostgreSQL solo guarda metadatos, propiedad y estado.
- Subida en streaming con lista blanca de tipos, límite de tamaño, sha256 y claves de almacenamiento generadas en servidor (defensa contra path traversal y colisiones).
- Archivado reversible vía estado; no hay borrado físico de documentos.

## IA (dirección)

- La IA es central en la dirección del producto pero siempre supervisada: asiste, estructura y propone; no decide.
- Toda llamada a APIs externas de IA o a un runtime privado de agentes pasa por el gateway interno (`app/assistant/gateway.py`); el pipeline de voz STT/TTS egresa solo por `app/assistant/speech.py` bajo la misma disciplina (ADR-021). Solo viaja el texto de la conversación, memoria institucional aprobada, campos que el usuario dicta, audio del turno y texto de respuesta sintetizable; los documentos originales no salen del servidor y los logs registran solo metadatos (runtime, modelo, tokens, bytes/duración/idioma), nunca contenido.
- Primera pieza implementada: Anacleto v2 (`app/assistant/`) es un único asistente model-first. No hay planner/router ni handlers de plantillas: el modelo redacta desde un system prompt con contrato de producto, organizaciones visibles, memoria aprobada, cobertura de ordenanzas y herramientas filtradas por permisos. El backend ejecuta las herramientas con los mismos chequeos RBAC que las rutas REST, persiste `agent_key="anacleto"`, `routing=null` y el rastro JSON de herramientas. Las escrituras siguen siendo supervisables: `create_requirement` tiene una guarda en código que exige confirmación en un turno posterior antes de crear el borrador. El bucle de tool-use usa el runtime configurado (`ASSISTANT_RUNTIME=anthropic` o `ASSISTANT_RUNTIME=hermes_agent`) y el egreso LLM pasa por `gateway.py`. Sin configuración completa del runtime seleccionado, el módulo queda deshabilitado (503).
- Streaming web: el endpoint SSE `/assistant/conversations/{id}/messages/stream` emite deltas de texto y actividad de herramientas; el POST clásico queda para compatibilidad y Telegram.
- Diálogo por voz web: el navegador captura con `MediaRecorder` y reproduce con `<audio>`. La transcripción usa `POST /assistant/audio-transcriptions` (runtime `disabled|nvidia_nim`) y la síntesis `POST /assistant/speech` (runtime `disabled|azure`, MP3 `audio/mpeg`). `/assistant/status` expone las banderas de STT/TTS para que el frontend oculte el modo voz cuando falte alguna. `input_mode="voice"` añade un bloque de estilo oral al prompt por turno, sin columnas nuevas. La web implementa un modo por turnos y un modo manos libres con parada por silencio, síntesis por frases, re-escucha opcional y pausa al ocultar la pestaña.
- Feedback interno del asistente: Anacleto puede usar `send_admin_feedback` cuando el usuario lo pida o confirme, siempre como herramienta auditada y con los permisos del usuario.
- Memoria institucional controlada: el asistente puede proponer entradas (`assistant.memory.propose`), pero solo quedan reutilizables tras aprobación humana (`assistant.memory.review`). La reutilización exige `assistant.memory.view` en la organización y solo inyecta entradas `approved` como contexto delimitado.

## MVP ordenanzas Burgos

- El primer corpus de demo se limita deliberadamente a pueblos de Burgos y a fuentes oficiales BOPBUR (`bopbur.diputaciondeburgos.es`); no representa cobertura completa de Castilla y León.
- `backend/app/ordinances/demo_bootstrap.py` carga un conjunto controlado de anuncios reales del BOP Burgos usando el flujo de `ordinance_import_jobs`, extrae texto de los PDFs oficiales, genera chunks y embeddings, y marca esos chunks como `approved` solo para recuperación demo. Cada ordenanza conserva `source_url`, `bulletin_number`, municipio, materia y una nota de revisión indicando que requiere validación jurídica humana antes de uso oficial.
- Para recargar el corpus demo en una DB local ya migrada: `cd backend && DATABASE_URL=postgresql+psycopg://app:app@127.0.0.1:5432/app PYTHONPATH=. python -m app.ordinances.demo_bootstrap`. El comando es idempotente por URL oficial.
- Go/no-go antes de una demo: verificar BOPBUR en `official_legal_sources`, ordenanzas BOPBUR aprobadas, chunks `review_status='approved'`, embeddings `embedding_status='ready'` y búsquedas positivas con `semantic_search_ordinances`. Las búsquedas aceptan filtros estructurados por `municipality_id`/`municipality_name` y `topic`; Anacleto recibe la cobertura disponible en el prompt y debe usar `semantic_search_ordinances` para contenido normativo. Si no hay chunk aprobado para un municipio/materia, debe reconocer falta de cobertura sin inventar normativa.
- Sprint 1 empieza la cobertura Burgos reproducible con un conector determinista BOPBUR en `backend/app/ordinances/bop_burgos.py`: consulta el formulario oficial `/busqueda`, parsea anuncios/PDFs oficiales sin recurrir a búsqueda web general, alimenta `ordinance_import_jobs` cuando la fuente activa es `bopbur.diputaciondeburgos.es`, trocea textos por artículos cuando existen marcadores legales y expone `GET /ordinances/coverage/burgos` como reporte operativo de municipios, ordenanzas, chunks listos, fallos de importación/OCR y fallos de embeddings. El reporte agrupa por nombre de municipio para evitar que altas duplicadas locales distorsionen la cobertura lógica. Los embeddings fallidos de Burgos se pueden reintentar de forma acotada con `POST /ordinances/coverage/burgos/retry-embeddings`.

## Frontend

- App Router multi-ruta con shell de navegación lateral: `/asistente`, `/requisitos`, `/proyectos`, `/mapa`, `/cuenta` y `/admin/{usuarios,grupos,organizaciones,roles,municipios,ordenanzas}`. Los ítems del menú usan los mismos predicados de permisos que las rutas; los permisos de lectura/compare de ordenanzas del alcalde piloto no abren administración, y `/proyectos` solo aparece con permisos de proyecto. Tras el login se aterriza en la primera sección visible.
- La sesión vive en `SessionProvider` (layout raíz): usuario, embudo de 401 → logout, cierre de sesión. Guard client-side; sin `middleware.ts` por ahora.
- Cada ruta monta solo su controlador de dominio y carga datos al entrar; las listas de otros dominios llegan por fetchers ligeros (`app/lib/fetchers.ts`). Los seis hooks de administración viven en `app/lib/admin/`.
- Selección, filtros y paginación viven en la URL (deep-links, refresh y botón atrás funcionan); los filtros de municipios, ordenanzas y requisitos se aplican en el servidor.
- Convenciones: sin librerías de UI/estado, TS estricto, texto en español, CSS monocromo propio.

## Tests

- pytest + httpx dentro del contenedor backend, montando el código fuente.
- Base de datos PostgreSQL de test separada (`app_test*`); cada test corre dentro de una transacción externa con savepoints, y se hace rollback al terminar — aislamiento total sin tocar datos de desarrollo.
- Cobertura prioritaria: matriz de permisos, aislamiento entre organizaciones y reglas de escalada (el núcleo de seguridad).

## Carencias conocidas (deuda aceptada conscientemente)

- Sin refresh tokens; la revocación server-side cubre solo el cambio/reset de contraseña (ADR-015): el logout no invalida el JWT, que expira a los 60 min.
- Los rate limiters (login, cambio de contraseña) son por proceso; al pasar a varios workers deben moverse a Redis (y valorar entonces un límite secundario por cuenta frente a password spraying, ADR-015).
- El guard de sesión del frontend es client-side; añadir `middleware.ts` si se quiere bloquear rutas antes de hidratar.
- Sin pipeline de CI; validación local según README §9.
- Contenedores sin hardening de producción (root, un worker, sin TLS); aceptable mientras todo siga en localhost.
