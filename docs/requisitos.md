# Requisitos

Actualizado: 2026-07-13. Este documento enumera principalmente capacidades ya implementadas. La visión objetivo y sus límites están en `docs/vision-producto.md`; el análisis de transición está en `docs/analisis-repositorio-2026-07-13.md`.

## Visión de producto

Anacleto es un agente operativo interno para cada ayuntamiento. Conversa con el alcalde y el resto de trabajadores, coordina personas y sistemas y puede actuar de forma proactiva dentro de competencias, políticas y delegaciones auditables.

La captura de requisitos deja de ser el propósito principal del asistente. Pasa a ser una capacidad de evolución del producto: Anacleto dialoga y contrasta la necesidad con los usuarios afectados antes de generar una propuesta estructurada y validada.

## Requisitos funcionales implementados

- Autenticación JWT con restauración de sesión (`/auth/me`) y bootstrap del primer administrador.
- Administración de usuarios, grupos, roles y permisos (RBAC de 29 códigos + superusuario).
- Organizaciones como tenants que delimitan los datos operativos.
- Proyectos (expedientes/áreas de trabajo) con miembros directos y por grupo.
- Documentos por proyecto: subida, metadatos, descarga y archivado; bytes en disco propio, metadatos en PostgreSQL.
- Requirements Intake: captura estructurada de necesidades con flujo de estados, prioridades e hilo de mensajes.
- Municipios: datos de referencia globales de municipios reales.
- Ordenanzas: registros estructurados vinculados a municipio y opcionalmente a un documento.
- Asistente de IA conversacional Anacleto v2: el usuario conversa en español con un único asistente model-first, con streaming web y Markdown. El asistente consulta datos visibles, usa herramientas filtradas por permisos y puede crear requisitos solo como borradores supervisables. `create_requirement` exige confirmación humana en un turno posterior mediante guarda backend, no solo por prompt. Toda llamada a IA externa o runtime privado pasa por el gateway interno (ver restricciones). Puede ejecutarse con Anthropic o con Hermes Agent como aplicación/runtime privado. Permiso de acceso: `assistant.use`.
- Diálogo por voz web con Anacleto: si STT y TTS están configurados, el usuario puede activar `Modo voz`, hablar al micrófono, enviar automáticamente la transcripción como turno de voz y escuchar la respuesta en español. El modo manos libres añade parada por silencio, síntesis por frases durante el streaming, re-escucha automática configurable (`assistant.voice.handsfree`) y pausa al ocultar la pestaña. Sin configuración de voz, la web conserva el flujo de texto.
- Memoria institucional controlada: el agente puede proponer conocimiento de organización, pero un responsable debe aprobarlo, editarlo, rechazarlo o bloquearlo antes de que sea reutilizable. La memoria oficial reside en PostgreSQL y se gobierna desde el backend propio, no en Hermes Agent. Permisos: `assistant.memory.propose`, `assistant.memory.view`, `assistant.memory.review`.

## Requisitos técnicos

- Backend FastAPI + SQLAlchemy + Alembic; frontend Next.js; PostgreSQL; orquestación local con Docker Compose.
- Redis/RQ para trabajos encolados de ordenanzas y oficina de agentes.
- Configuración exclusivamente por variables de entorno; sin secretos en el repositorio.
- Tests de backend con pytest ejecutados dentro del contenedor contra una base de datos PostgreSQL de test aislada (ver README §9).

## Restricciones

- El estado implementado exige supervisión para las escrituras disponibles. El objetivo es sustituir esa regla general por niveles de autonomía basados en riesgo, competencia y delegación, manteniendo aprobación obligatoria donde corresponda.
- El estado implementado no envía documentos originales ni datos municipales sensibles a APIs de IA. El objetivo permite que proveedores aprobados contractualmente procesen los datos necesarios, pero solo después de implementar política por proveedor, minimización, trazabilidad y los controles descritos en la visión de producto.
- Los documentos se almacenan en servidor propio, nunca en S3/almacenamiento externo.
- Los objetos de negocio importantes se archivan, no se borran (usuarios y grupos son la excepción: borrado físico con guardas).
- Backend y frontend ligados a localhost en desarrollo; PostgreSQL y Redis nunca expuestos públicamente.
- La concesión de superusuario, la gestión de roles/permisos globales y la creación de organizaciones (tenants) son operaciones reservadas a superusuarios.
