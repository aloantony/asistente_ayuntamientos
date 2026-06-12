# Arquitectura

Actualizado: 2026-06-12.

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

- Autenticación: JWT HS256 de acceso (60 min), sin refresh; contraseñas con Argon2id.
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
- Toda llamada a APIs externas de IA pasa por el gateway interno (`app/assistant/gateway.py`, punto único de salida): solo viaja el texto de la conversación y los campos que el usuario dicta; los documentos originales no salen del servidor y los logs registran solo metadatos (modelo, tokens), nunca contenido.
- Primera pieza implementada: el agente conversacional de intake de requisitos (`app/assistant/`). Bucle síncrono de tool-use contra la API de Claude (modelo configurable, por defecto `claude-opus-4-8`); las herramientas del agente ejecutan las mismas validaciones RBAC que las rutas REST, los requisitos se crean siempre como borrador con `source_type=conversation`, y cada mensaje del asistente guarda un rastro JSON de las herramientas ejecutadas. Conversaciones y mensajes persisten en PostgreSQL y son privados de su autor. Sin `ANTHROPIC_API_KEY` el módulo queda deshabilitado (503).

## Tests

- pytest + httpx dentro del contenedor backend, montando el código fuente.
- Base de datos PostgreSQL de test separada (`app_test*`); cada test corre dentro de una transacción externa con savepoints, y se hace rollback al terminar — aislamiento total sin tocar datos de desarrollo.
- Cobertura prioritaria: matriz de permisos, aislamiento entre organizaciones y reglas de escalada (el núcleo de seguridad).

## Carencias conocidas (deuda aceptada conscientemente)

- Sin refresh tokens, revocación, cambio/reset de contraseña ni rate limiting en login.
- Sin paginación en los listados (bloqueante para importar el dataset INE completo).
- Token JWT en localStorage en el frontend.
- Sin pipeline de CI; validación local según README §9.
- Contenedores sin hardening de producción (root, un worker, sin TLS); aceptable mientras todo siga en localhost.
