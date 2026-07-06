# Requisitos

Actualizado: 2026-07-03. Este documento describe el estado actual del producto. La dirección de próximos tramos vive en el roadmap del `README.md`; el diseño del asistente se detalla en `docs/diseno-multiagente.md`.

## Alcance actual

Asistente Ayuntamientos es una plataforma para ayuntamientos pequeños y medianos que combina gestión interna, captura de necesidades, documentación, ordenanzas, mapa municipal e IA supervisada.

El producto tiene dos pilares visibles:

- Anacleto, el asistente municipal conversacional y supervisado.
- Mapa, planos e información territorial del pueblo.

## Seguridad, usuarios y tenancy

- Autenticación JWT con cookie httpOnly para navegador, soporte Bearer para API/tests, `/auth/me`, `/auth/logout`, cambio de contraseña, reset administrativo y bootstrap del primer administrador.
- Revocación de tokens emitidos antes del último cambio/reset de contraseña.
- Rate limiting en memoria para login y cambio de contraseña.
- Administración de usuarios, grupos, roles y permisos con RBAC multi-tenant.
- Organizaciones como tenants que delimitan usuarios, grupos, proyectos, documentos, requisitos, memoria y tareas internas.
- Catálogo de permisos sembrado automáticamente al arrancar el backend.
- Operaciones globales reservadas a superusuarios: superusuario, roles/permisos y creación de organizaciones.

## Gestión municipal

- Proyectos como expedientes, áreas de trabajo o iniciativas dentro de una organización.
- Documentos por proyecto: subida, metadatos, descarga y archivado. Los bytes se guardan en almacenamiento propio y PostgreSQL conserva metadatos.
- Municipios como datos de referencia globales.
- Requisitos/necesidades con organización, proyecto opcional, prioridad, estado, campos descriptivos e hilo de mensajes.
- Ordenanzas vinculadas a municipios y opcionalmente a documentos accesibles por el usuario.

## Ordenanzas y corpus BOPBUR

- Importación jurídica supervisada mediante trabajos de importación de ordenanzas.
- Fuentes oficiales configuradas, con BOP de Burgos como fuente primaria del MVP Burgos.
- Ordenanzas importadas en `pending_review`; la aprobación exige permisos humanos.
- Chunks legales citables y embeddings para búsqueda semántica interna.
- Reporte de cobertura Burgos y reintento acotado de embeddings fallidos.
- Consulta asistida de ordenanzas internas aprobadas con permiso `ordinances.compare`, reconociendo falta de cobertura cuando no hay resultados suficientes.

## Mapa, planos e información territorial

- Dominio `geo` compartido para ubicaciones reutilizables (`GeoLocation`) y vínculos con entidades (`EntityLocation`).
- Entidades geolocalizables v1: necesidades y proyectos.
- Mapa municipal en frontend sobre `/mapa`, con permisos `map.view`, `map.edit`, `map.import` y `map.manage`.
- La visibilidad combina permiso de mapa y visibilidad normal de la entidad, para evitar filtraciones entre organizaciones.
- GeoJSON compatible con RFC 7946; PostGIS queda aplazado.

## Anacleto y captura conversacional

- Anacleto conversa en español y puede consultar información visible, estructurar necesidades, proponer borradores, buscar ordenanzas internas, consultar mapa, proponer memoria y preparar tareas supervisadas.
- La captura de necesidades sigue ADR-020: conversación, propuesta estructurada y confirmación explícita antes de escribir.
- Una necesidad solo se crea cuando el usuario confirma el borrador o pide guardarlo explícitamente con título y problema mínimos. El registro se crea como borrador (`status=draft`, `source_type=conversation`).
- El planificador semántico puede entender intención y referencias naturales, pero el backend conserva permisos, visibilidad, duplicados, acciones auditadas y bloqueo de escrituras no confirmadas.
- Las conversaciones y acciones del asistente quedan persistidas y auditadas. Permiso base: `assistant.use`.
- El runtime puede ser Anthropic o Hermes Agent privado, siempre detrás del gateway interno.
- La búsqueda web controlada usa una instancia Hermes separada y exige `assistant.web.search`.

## Oficina de agentes

- La oficina de agentes añade tareas internas delegables para Anacleto, sin mostrar varias voces al usuario.
- Las tareas tienen organización, departamento, prioridad, política de aprobación, estado y eventos.
- Los permisos `agent_office.view`, `agent_office.create`, `agent_office.approve`, `agent_office.execute` y `agent_office.manage` gobiernan visibilidad, creación, aprobación y ejecución.
- Las acciones mutantes requieren aprobación según la política de la tarea.
- La rutina v1 es `daily_briefing`, disparable manualmente en desarrollo.

## Memoria institucional y funcionalidades transversales

- El asistente puede proponer memoria institucional por organización, pero solo las entradas aprobadas por una persona con `assistant.memory.review` se reutilizan.
- La memoria oficial vive en PostgreSQL y no se delega al runtime de IA.
- Las entradas aprobadas se inyectan como contexto delimitado solo cuando el usuario tiene `assistant.memory.view`.
- Anacleto puede proponer funcionalidades transversales a partir de necesidades visibles usando resúmenes anonimizados, y registrar aceptación por organización tras permiso explícito.

## Telegram

- Telegram funciona como canal del asistente para usuarios ya autenticados.
- El usuario genera un código de vinculación de un solo uso desde la cuenta web y lo envía al bot.
- Las conversaciones Telegram se guardan con canal separado y usan el mismo RBAC del usuario vinculado.
- Telegram está deshabilitado por defecto y se activa con variables de entorno.

## Requisitos técnicos

- Backend FastAPI + SQLAlchemy + Alembic.
- Frontend Next.js App Router.
- PostgreSQL como base de datos principal.
- Redis declarado para colas/caché y usado por trabajos cuando procede.
- Docker Compose para desarrollo local.
- Configuración exclusivamente por variables de entorno; sin secretos en el repositorio.
- Tests de backend con pytest dentro del contenedor contra base PostgreSQL de test aislada.

## Restricciones

- La IA asiste, propone y estructura, pero no toma decisiones legales o administrativas finales sin supervisión humana.
- Ningún documento original ni dato municipal sensible se envía directamente a APIs de IA externas ni al runtime de agentes; toda llamada pasa por el gateway de IA, con minimización de datos.
- Los documentos se almacenan en servidor propio, nunca en S3/almacenamiento externo.
- Los objetos de negocio importantes se archivan, no se borran (usuarios y grupos son la excepción: borrado físico con guardas).
- Backend y frontend ligados a localhost en desarrollo; PostgreSQL y Redis nunca expuestos públicamente.
- La concesión de superusuario, la gestión de roles/permisos globales y la creación de organizaciones (tenants) son operaciones reservadas a superusuarios.
