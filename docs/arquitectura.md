# Arquitectura

Actualizado: 2026-06-15.

## Visión general

Aplicación multi-tenant con cuatro servicios en Docker Compose:

- `backend`: API HTTP FastAPI (puerto 127.0.0.1:8000), monolito modular.
- `frontend`: Next.js App Router (puerto 127.0.0.1:3000), consola de administración y trabajo.
- `postgres`: PostgreSQL 17, interno (sin puerto publicado), con volumen persistente.
- `redis`: Redis 7, interno, reservado para colas/caché futuras (sin consumidor todavía).

## Modelo de dominio y tenancy

La distinción central del dominio:

- `Organization` es el **tenant**: la entidad cliente que usa la aplicación. Delimita usuarios (vía membresía), grupos, proyectos, documentos y requisitos.
- `Municipality` es **dato de referencia global**: municipios reales de España, compartidos entre tenants, base de la futura base de conocimiento comparativa. Una organización puede enlazar opcionalmente con un municipio.
- `Project` (expediente/área de trabajo) y `Document` viven dentro de una organización. `Requirement` pertenece a una organización y opcionalmente a un proyecto.
- `Ordinance` es global, pertenece a un municipio y puede enlazar a un documento de un tenant; ese enlace exige que quien lo crea tenga acceso al documento.

## Control de acceso

- Autenticación: JWT HS256 de acceso (60 min, con `iat`) entregado en cookie httpOnly SameSite=Lax al navegador (`POST /auth/logout` la limpia y exige sesión); la cabecera Bearer sigue aceptada para API/tests. Contraseñas con Argon2id, nunca recortadas; cambio self-service (`POST /auth/change-password`, reemite la cookie) y reset por administradores (con guarda: solo superusuarios resetean a superusuarios); ambos revocan los tokens emitidos antes (`iat` vs `users.password_changed_at`, ADR-015). Rate limiting en memoria por cliente+cuenta en login y cambio de contraseña: solo los intentos fallidos consumen cupo.
- Autorización: cadena RBAC usuario → grupo → rol → permiso. Los permisos de un grupo solo cuentan si el usuario es además miembro de la organización del grupo, lo que hace el modelo consciente del tenant.
- `is_superuser` puentea todos los chequeos. Conceder o retirar superusuario es operación de superusuarios.
- Operaciones globales reservadas a superusuarios: crear/editar/borrar roles y permisos, asignar permisos a roles, crear organizaciones (tenants).
- `users.manage` está delimitado por organización: un administrador solo gestiona usuarios que comparten alguna organización donde él tiene el permiso.
- Municipios y ordenanzas son globales: sus permisos (`municipalities.*`, `ordinances.*`) se evalúan sin filtro de organización; quién debe curarlos es una decisión de producto abierta.
- El catálogo de permisos se siembra automáticamente al arrancar el backend (idempotente); `POST /admin/permissions/bootstrap` sigue disponible como re-siembra manual.

## Documentos

- Los bytes se guardan en el sistema de archivos (`DOCUMENT_STORAGE_ROOT`, volumen Docker `document_storage`); PostgreSQL solo guarda metadatos, propiedad y estado.
- Subida en streaming con lista blanca de tipos, límite de tamaño, sha256 y claves de almacenamiento generadas en servidor (defensa contra path traversal y colisiones).
- Archivado reversible vía estado; no hay borrado físico de documentos.

## IA (dirección)

- La IA es central en la dirección del producto pero siempre supervisada: asiste, estructura y propone; no decide.
- Toda llamada a APIs externas de IA o a un runtime privado de agentes pasa por el gateway interno (`app/assistant/gateway.py`, punto único de salida): solo viaja el texto de la conversación, memoria institucional aprobada y los campos que el usuario dicta; los documentos originales no salen del servidor y los logs registran solo metadatos (runtime, modelo, tokens), nunca contenido.
- Primera pieza implementada: el asistente conversacional (`app/assistant/`) con registro declarativo de agentes (`requirements_intake` y `consultation`) y catálogo backend de herramientas. Cada turno selecciona un agente; si `ASSISTANT_PLANNER_RUNTIME=hermes_agent`, Hermes Agent puede actuar como planner/router privado, pero solo propone el agente. El backend filtra las herramientas permitidas por agente, ejecuta los mismos chequeos RBAC que las rutas REST y persiste `agent_key`, `routing` y el rastro JSON de herramientas. El bucle síncrono de tool-use usa el runtime configurado (`ASSISTANT_RUNTIME=anthropic` o `ASSISTANT_RUNTIME=hermes_agent`). En modo Hermes Agent, el backend llama al API Server privado compatible con OpenAI; Hermes Agent actúa como aplicación/runtime o planner, no como base de datos de memoria ni como autoridad de permisos. Los requisitos se crean siempre como borrador con `source_type=conversation`. Conversaciones y mensajes persisten en PostgreSQL y son privados de su autor. Sin configuración completa del runtime seleccionado, el módulo queda deshabilitado (503).
- Memoria institucional controlada: el asistente puede proponer entradas (`assistant.memory.propose`), pero solo quedan reutilizables tras aprobación humana (`assistant.memory.review`). La reutilización exige `assistant.memory.view` en la organización y solo inyecta entradas `approved` como contexto delimitado.

## Frontend

- App Router multi-ruta con shell de navegación lateral: `/asistente`, `/requisitos`, `/proyectos`, `/cuenta` y `/admin/{usuarios,grupos,organizaciones,roles,municipios,ordenanzas}`. Los ítems del menú usan los mismos predicados de permisos que las rutas; tras el login se aterriza en la primera sección visible.
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
