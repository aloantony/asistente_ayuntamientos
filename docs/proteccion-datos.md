# Protección de datos en el piloto

Actualizado: 2026-07-30

Documento de trabajo para el piloto cerrado con datos municipales reales.
No es un dictamen jurídico: recoge qué datos trata la aplicación, qué medidas
existen hoy en el código y qué falta por cerrar antes de ampliar el uso. Las
decisiones técnicas asociadas están en ADR-035, ADR-036 y ADR-037.

## Alcance actual

Piloto con **dos usuarios** (el desarrollador y el alcalde socio) sobre un VPS
propio en la UE, con datos reales de un ayuntamiento. Responsable del tratamiento:
el ayuntamiento. La plataforma actúa como herramienta bajo su control.

## Qué datos personales hay

Sorprendentemente pocos campos estructurados: **no existe ningún campo de DNI/NIF,
teléfono, dirección postal ni fecha de nacimiento** en el esquema.

| Dónde | Datos | Notas |
|---|---|---|
| `users` | correo, nombre completo, contraseña con hash Argon2id | Único registro de identidad |
| `telegram_user_links` | identificadores de cuenta de Telegram | Solo si se activa Telegram (hoy desactivado) |
| `geo_locations` | dirección, topónimo, **referencia catastral**, coordenadas | La referencia catastral apunta a una finca y, vía Catastro, a su titular |
| `security_events` | IP, correo intentado, user-agent | Traza de auditoría; retención 90 días |
| Campos de texto libre | nombres y detalles de vecinos que el usuario escriba | Es aquí donde está el riesgo real |

Los campos de texto libre que en la práctica contendrán datos de personas:
`assistant_messages.content` y `.actions`, `assistant_memory_entries.content`,
`requirements.*` (incluidos `affected_users` y `data_sensitivity_notes`),
`requirement_messages.body`, `maintenance_orders.title/description`,
`maintenance_order_events.note`, `municipal_assets.notes`, `municipal_blocks.body`,
`agent_office_tasks.*`, `documents.original_filename` y el texto de ordenanzas
importadas de boletines oficiales, que rutinariamente incluye nombres y DNI
parciales.

Las referencias entre entidades usan **identificadores internos de usuario**, no
copias de sus datos.

## Medidas técnicas que existen hoy

- **Acceso**: usuario → grupo → rol → permiso, con aislamiento por organización.
  Contraseñas con Argon2id. Sesión en cookie `httpOnly`, `SameSite=Lax` y `Secure`
  en producción. Cambiar la contraseña revoca los tokens anteriores.
- **Fuerza bruta**: límite por (IP, cuenta), techo por IP y límite en el endpoint
  de bootstrap. Los bloqueos se registran.
- **Traza de accesos**: `security_events`, inmutable por trigger de base de datos,
  registra logins (correctos, fallidos y bloqueados), cambios y reseteos de
  contraseña, altas y bajas de superusuario, borrados de usuario y **subida,
  descarga y archivado de documentos**. Solo metadatos: nunca el contenido.
- **Transporte**: TLS obligatorio con certificados renovados automáticamente; HSTS,
  CSP y el resto de cabeceras de seguridad.
- **Minimización en la salida a la IA**: todas las llamadas a proveedores externos
  pasan por puntos de egreso únicos y auditados (`gateway.py`, `speech.py`,
  `web_search.py`). **Nunca se envían documentos originales ni ficheros municipales
  almacenados.** Los logs guardan solo metadatos: runtime, modelo, motivo de parada
  y recuento de tokens. La búsqueda web tiene además un filtro que rechaza consultas
  con DNI, NIE, correos o teléfonos españoles.
- **Clasificación de la memoria del asistente**: las entradas llevan sensibilidad
  (`normal`, `personal`, `sensitive`, `legal`) y estado, solo se reinyectan las
  aprobadas por una persona, y el estado `blocked` corresponde al bloqueo de datos
  del art. 32 LOPDGDD.
- **Copias**: diarias, con manifiesto de integridad y procedimiento de
  restauración ensayado (ADR-037).

## Retención

| Clase | Plazo | Estado |
|---|---|---|
| Traza de seguridad (`security_events`) | 90 días | **Automatizado** (`ops/purge_security_events.sh`, diario) |
| Códigos de vinculación de Telegram | 10 minutos | Automatizado |
| Copias de seguridad | 7 diarias, 4 semanales, 3 mensuales | Automatizado |
| Conversaciones del asistente y su memoria | Por definir con el ayuntamiento | **Pendiente**: no hay purga automática |
| Documentos y expedientes | Plazo institucional, por acordar | **Pendiente** |
| Órdenes de mantenimiento | Mientras formen parte del historial del activo | **Pendiente** de plazo formal |

Que las tres últimas filas estén pendientes es una carencia consciente, no un
olvido: el plazo no lo puede decidir el desarrollador, lo tiene que fijar el
ayuntamiento.

## Tres conflictos reales que conviene conocer

Detectados al revisar el esquema. No son fallos, son consecuencias de decisiones
previas, y afectan a cómo se atiende un derecho de supresión:

1. **Un usuario que haya intervenido en una orden de mantenimiento no se puede
   borrar físicamente.** `maintenance_orders.created_by_id` y
   `maintenance_order_events.actor_id` son `RESTRICT NOT NULL` para que la
   auditoría no pierda al actor. La vía correcta es desactivar la cuenta, no
   borrarla, y documentarlo como limitación por necesidad de auditoría.
2. **Borrar una conversación no borra el texto derivado de ella.** Las entradas de
   memoria, requisitos, tareas y comentarios creados a partir de una conversación
   guardan su procedencia con `ON DELETE SET NULL`: al borrar la conversación
   quedan huérfanos, no eliminados. Una supresión efectiva exige recorrer también
   esas tablas.
3. **El título de la conversación es una copia de los primeros 255 caracteres del
   primer mensaje**, y se muestra en todos los listados. Si el primer mensaje lleva
   datos personales, están duplicados en un campo que se ve en muchos sitios.

## Cifrado en reposo

No hay cifrado a nivel de columna ni de base de datos. La postura del piloto es
**cifrado de disco del VPS** (activarlo al crear el servidor si el proveedor lo
ofrece), permisos estrictos en el fichero de secretos y copias con permisos `0600`.
Se documenta así en lugar de aparentar una protección que no existe. Las copias
**no se cifran** porque no salen del servidor; si algún día salen, tienen que
cifrarse antes (la costura está preparada en `ops/backup.sh`).

## Encargados del tratamiento y subencargados

| Proveedor | Para qué | Estado |
|---|---|---|
| Proveedor del VPS | Alojamiento | Debe estar en la UE. Contrato de encargado pendiente de firma |
| Anthropic **u** OpenAI | Runtime del asistente | **Puerta bloqueante**: contrato de encargado firmado y región documentada antes de datos reales |
| Azure Speech / NVIDIA | Voz | Desactivados. No activar sin contrato |
| Brave Search | Búsqueda web | Desactivada. `BRAVE_SEARCH_STORAGE_RIGHTS_CONFIRMED=false` hasta cerrar retención |
| Telegram | Canal alternativo | Desactivado |

El puente `codex_subscription` está **prohibido en producción** y el backend se
niega a arrancar con él (ADR-024). No es un runtime con contrato: es una vía local
de evaluación.

## Puertas pendientes antes de ampliar el piloto

En orden de importancia:

1. **Contrato de encargado del tratamiento** con el proveedor de IA elegido, con
   región de tratamiento y política de retención documentadas. Sin esto no deberían
   entrar datos reales en el asistente. El resto de la aplicación funciona con el
   asistente sin configurar (devuelve 503 por diseño).
2. **Evaluación de impacto (DPIA)** antes de abrir a más ayuntamientos o a más
   usuarios que los dos del piloto.
3. **Plazos de retención** de conversaciones, documentos y órdenes, acordados con
   el ayuntamiento, y su automatización.
4. **Registro de actividades de tratamiento** del ayuntamiento actualizado con esta
   herramienta.
5. **Aviso de privacidad** dentro de la aplicación. Con dos usuarios internos se
   puede posponer; con personal municipal, no.
6. **Procedimiento de brecha**: quién decide, cómo se documenta y notificación a la
   AEPD en 72 horas. Hoy la traza de seguridad permite reconstruir lo ocurrido, que
   es la parte técnica; falta la parte organizativa.
7. **RR. HH. sigue excluido** del alcance, por contener datos personales y laborales
   de especial sensibilidad, hasta que haya DPIA específica (ADR-019).
