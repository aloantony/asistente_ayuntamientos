# Requisitos

Actualizado: 2026-07-17. Este documento enumera principalmente capacidades ya implementadas. La visión objetivo y sus límites están en `docs/vision-producto.md`; la fuente de detalle operativo es `README.md`.

## Visión de producto

Anacleto es un agente operativo interno para cada ayuntamiento. Conversa primero con el alcalde y después con el resto de trabajadores, coordina personas y sistemas y puede actuar de forma proactiva dentro de competencias, políticas y delegaciones auditables.

La captura de requisitos no es el propósito principal del asistente. Es una capacidad de evolución del producto: Anacleto dialoga y contrasta la necesidad antes de generar una propuesta estructurada que el usuario pueda corregir y validar.

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
- Lienzo documental de Anacleto: cada conversación privada puede mantener varios borradores Markdown no oficiales y uno activo. La persona y el asistente pueden crear, editar y restaurar revisiones con concurrencia optimista, autoguardado, vista previa segura e historial. Las escrituras del asistente son `draft_write/direct`; nunca publican, aprueban ni convierten el contenido en `Document` u `Ordinance`. Contrato: `docs/lienzo-documentos.md`.
- Diálogo por voz web con Anacleto: si STT y TTS están configurados, el usuario puede activar `Modo voz`, hablar al micrófono, enviar automáticamente la transcripción como turno de voz y escuchar la respuesta en español. El modo manos libres añade parada por silencio, síntesis por frases durante el streaming, re-escucha automática configurable (`assistant.voice.handsfree`) y pausa al ocultar la pestaña. Sin configuración de voz, la web conserva el flujo de texto.
- Memoria institucional controlada: el agente puede proponer conocimiento de organización, pero un responsable debe aprobarlo, editarlo, rechazarlo o bloquearlo antes de que sea reutilizable. La memoria oficial reside en PostgreSQL y se gobierna desde el backend propio, no en Hermes Agent. La revisión municipal se realiza en `/admin/memoria`, aislada por organización y protegida frente a ediciones concurrentes. Permisos: `assistant.memory.propose`, `assistant.memory.view`, `assistant.memory.review`.
- Feedback de producto confirmado: el envío exige una confirmación explícita ligada al contenido exacto. El desarrollador lo revisa en la bandeja local `/admin/producto`, reservada a superusuarios. Esta bandeja piloto no anonimiza ni transmite todavía la información a un control central.

## Requisitos técnicos

- Backend FastAPI + SQLAlchemy + Alembic; frontend Next.js; PostgreSQL; orquestación local con Docker Compose.
- Redis declarado en Compose, reservado para colas/caché de fases futuras (sin consumidor en el código todavía).
- Configuración exclusivamente por variables de entorno; sin secretos en el repositorio.
- Tests de backend con pytest ejecutados dentro del contenedor contra una base de datos PostgreSQL de test aislada (ver README §9).

## Restricciones

- Las escrituras de negocio usan autorización explícita de un solo uso. La excepción implementada `draft_write/direct` se limita a borradores personales, no oficiales, versionados y reversibles del lienzo. La autonomía futura seguirá graduándose por riesgo, competencia y delegación.
- Los archivos originales subidos como `Document` no se envían a APIs de IA. El cuerpo Markdown de un borrador sí puede llegar al runtime conversacional aprobado cuando Anacleto lo lee expresamente; una edición manual no provoca egreso y el cuerpo se redacta de eventos, acciones y estado Realtime. El contenido del lienzo y la web pública no se mezclan en el mismo turno.
- Los documentos se almacenan en servidor propio, nunca en S3/almacenamiento externo.
- Los objetos de negocio importantes se archivan, no se borran (usuarios y grupos son la excepción: borrado físico con guardas).
- Backend y frontend ligados a localhost en desarrollo; PostgreSQL y Redis nunca expuestos públicamente.
- La concesión de superusuario, la gestión de roles/permisos globales y la creación de organizaciones (tenants) son operaciones reservadas a superusuarios.
