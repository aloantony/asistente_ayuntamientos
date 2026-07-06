# Requisitos

Actualizado: 2026-06-25. Este documento refleja el estado actual del producto. La fuente de detalle operativo es `README.md`; los requisitos nuevos entran por el módulo de Requirements Intake.

## Visión de producto

Plataforma para ayudar a ayuntamientos pequeños y medianos a gestionar documentación, requisitos, ordenanzas y procesos internos, con apoyo progresivo de IA supervisada por humanos.

El primer usuario real es un alcalde (socio del proyecto) que comunicará las necesidades del producto mediante un agente de IA conversacional de intake de requisitos. El agente de intake ya existe como módulo central y la comparación de ordenanzas ha arrancado como flujo supervisado; las siguientes prioridades se refinan mediante Requirements Intake y feedback de usuarios municipales.

## Requisitos funcionales implementados

- Autenticación JWT con restauración de sesión (`/auth/me`) y bootstrap del primer administrador.
- Administración de usuarios, grupos, roles y permisos (RBAC de 29 códigos + superusuario).
- Organizaciones como tenants que delimitan los datos operativos.
- Proyectos (expedientes/áreas de trabajo) con miembros directos y por grupo.
- Documentos por proyecto: subida, metadatos, descarga y archivado; bytes en disco propio, metadatos en PostgreSQL.
- Requirements Intake: captura estructurada de necesidades con flujo de estados, prioridades e hilo de mensajes.
- Municipios: datos de referencia globales de municipios reales.
- Ordenanzas: registros estructurados vinculados a municipio y opcionalmente a un documento.
- Importación y comparación de ordenanzas: trabajos supervisados con Redis/RQ que consultan fuentes oficiales configuradas, crean ordenanzas en `pending_review`, fragmentan el texto legal en unidades citables/vectorizables y exponen una matriz temática entre municipios. La aprobación final exige revisión humana con `ordinances.review`.
- Asistente de IA conversacional de intake de requisitos: el usuario conversa en español y el agente crea y actualiza requisitos en su nombre (siempre como borradores supervisables), respetando sus permisos RBAC y dejando rastro auditable de cada acción. Toda llamada a IA externa o runtime privado pasa por el gateway interno (ver restricciones). Puede ejecutarse con Anthropic o con Hermes Agent como aplicación/runtime privado. Permiso de acceso: `assistant.use`.
- Memoria institucional controlada: el agente puede proponer conocimiento de organización, pero un responsable debe aprobarlo, editarlo, rechazarlo o bloquearlo antes de que sea reutilizable. La memoria oficial reside en PostgreSQL y se gobierna desde el backend propio, no en Hermes Agent. Permisos: `assistant.memory.propose`, `assistant.memory.view`, `assistant.memory.review`.
- Búsqueda web controlada desde el asistente: usa una instancia/perfil Hermes separado con solo herramienta web y permiso `assistant.web.search`; solo se envía la consulta explícita y la acción queda auditada.
- Canal Telegram del asistente: usuarios existentes pueden vincular un chat con código de un solo uso y operar el asistente con las mismas autorizaciones RBAC, manteniendo el canal separado en auditoría.

## Requisitos técnicos

- Backend FastAPI + SQLAlchemy + Alembic; frontend Next.js; PostgreSQL; orquestación local con Docker Compose.
- Redis declarado en Compose y usado por RQ para importación/comparación de ordenanzas; queda reservado también para caché de fases futuras.
- Embeddings legales almacenados en PostgreSQL; se usa `pgvector` cuando está disponible y embeddings deterministas locales en desarrollo.
- Configuración exclusivamente por variables de entorno; sin secretos en el repositorio.
- Tests de backend con pytest ejecutados dentro del contenedor contra una base de datos PostgreSQL de test aislada (ver README §9).

## Restricciones

- La IA asiste, propone y estructura, pero no toma decisiones legales o administrativas finales sin supervisión humana.
- Ningún documento original ni dato municipal sensible se envía directamente a APIs de IA externas ni al runtime de agentes; toda llamada pasa por el gateway de IA, con minimización de datos.
- Los documentos se almacenan en servidor propio, nunca en S3/almacenamiento externo.
- Los objetos de negocio importantes se archivan, no se borran (usuarios y grupos son la excepción: borrado físico con guardas).
- Backend y frontend ligados a localhost en desarrollo; PostgreSQL y Redis nunca expuestos públicamente.
- La concesión de superusuario, la gestión de roles/permisos globales y la creación de organizaciones (tenants) son operaciones reservadas a superusuarios.
