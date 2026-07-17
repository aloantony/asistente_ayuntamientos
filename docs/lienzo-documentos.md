# Lienzo de documentos de trabajo

Estado de referencia: 2026-07-17.

## Propósito y frontera oficial

El lienzo es una superficie Markdown asociada a una conversación privada de
Anacleto. Permite que una persona y el asistente desarrollen juntos borradores
como ordenanzas municipales, reglamentos, informes, cartas y actas.

Todo documento del lienzo se presenta como **«Borrador · No oficial»**. Crear,
editar, restaurar, copiar o descargar un borrador no crea ni modifica un
`Document`, una `Ordinance`, un expediente, el corpus jurídico o un acto de
aprobación, firma, publicación o registro. La promoción a cualquiera de esos
dominios requiere en el futuro un flujo separado y supervisado.

## Ámbito, propiedad y permisos

- Todas las rutas y herramientas exigen `assistant.use`.
- El propietario de la conversación es el único usuario con acceso. Un id de
  otra persona responde con el mismo `404` que un id inexistente.
- `organization_id` es opcional y solo clasifica el contexto. Se valida contra
  las organizaciones accesibles, pero no comparte el borrador con sus miembros.
- Una conversación puede tener varios borradores y un único borrador activo.
- Una conversación archivada permite lectura, pero no nuevas creaciones ni
  mutaciones.
- La primera versión ofrece colaboración persona–Anacleto, no coedición entre
  trabajadores ni edición simultánea.

## Persistencia y revisiones

El dominio usa tablas propias, separadas de los documentos subidos y de las
ordenanzas oficiales:

- `assistant_canvas_documents`: título, tipo, Markdown vigente, estado,
  revisión actual, conversación y organización contextual opcional.
- `assistant_canvas_revisions`: instantáneas inmutables de título y cuerpo,
  SHA-256, origen (`user`, `assistant` o `restore`), actor, mensaje/tool call y
  resumen del cambio.

La base protege las instantáneas mediante un trigger: no admite cambiar ni
eliminar directamente una revisión ya creada. Las revisiones solo desaparecen
por la cascada de ciclo de vida al eliminar su documento, conversación o
propietario. El `downgrade` de la migración se niega si existe algún borrador o
revisión, para no destruir contenido silenciosamente.

El título admite 255 caracteres y el cuerpo 60.000. Una creación sin contenido
genera una plantilla; las ordenanzas reciben una estructura inicial con
exposición de motivos, articulado y disposiciones finales.

Cada creación, edición, restauración o cambio de estado crea una revisión.
Restaurar nunca reescribe la historia: copia la instantánea elegida en una
revisión nueva. El endpoint de historial devuelve como máximo las 50 revisiones
más recientes, con extracto y hash, no el cuerpo histórico completo.

## Concurrencia e idempotencia

`PATCH` y restauración requieren `expected_revision`. Si el servidor ya avanzó,
responden `409`; la interfaz conserva el texto local y obliga a elegir entre la
revisión del servidor y reaplicar expresamente el texto local sobre ella.

`creation_id` y `mutation_id` hacen idempotentes los reintentos. La interfaz
reutiliza el mismo identificador mientras no cambie la operación lógica. En las
herramientas, el identificador se deriva de conversación, mensaje de usuario y
`tool_call_id`. Cada identificador queda ligado además al SHA-256 del payload
normalizado: reutilizarlo con otros argumentos devuelve `409`, no recupera ni
ejecuta una operación diferente. Antes de una mutación del asistente se bloquea
la conversación: un turno desplazado por un mensaje posterior no puede editar
el borrador.

## API

| Operación | Endpoint |
| --- | --- |
| Espacio y borrador activo | `GET /assistant/conversations/{id}/canvas` |
| Incluir archivados | El mismo endpoint con `include_archived=true` |
| Crear | `POST /assistant/conversations/{id}/canvas/documents` |
| Seleccionar el activo | `PUT /assistant/conversations/{id}/canvas/active` |
| Leer | `GET /assistant/canvas/documents/{id}` |
| Editar o archivar | `PATCH /assistant/canvas/documents/{id}` |
| Historial | `GET /assistant/canvas/documents/{id}/revisions` |
| Restaurar | `POST /assistant/canvas/documents/{id}/revisions/{revision}/restore` |

## Herramientas de Anacleto

Anacleto dispone de:

- `list_canvas_documents`
- `get_canvas_document`
- `create_canvas_document`
- `update_canvas_document`
- `list_canvas_revisions`
- `restore_canvas_revision`

Las lecturas declaran `side_effect=none` y `approval_policy=never`. Crear,
actualizar y restaurar declaran `side_effect=draft_write` y
`approval_policy=direct`: no piden una segunda confirmación porque afectan a un
borrador personal, no oficial, versionado y reversible. Esta excepción está
limitada por código al dominio del lienzo. Las escrituras oficiales o de base de
datos ajenas al lienzo mantienen autorización explícita de un solo uso.

Antes de editar o restaurar, el modelo debe leer la revisión vigente con
`get_canvas_document` en ese mismo turno y enviar exactamente esa
`expected_revision`; el backend lo exige, no depende solo del prompt. Una
operación correcta devuelve una acción estructurada
`ui.open_canvas_document` versión 1; el cliente la valida estrictamente y abre
el documento correspondiente.

## Privacidad, egreso y auditoría

El cuerpo completo vive en PostgreSQL tanto en el documento vigente como en sus
revisiones. Una edición manual por REST no llama al modelo. Cuando Anacleto
necesita leer el cuerpo, se entrega al runtime conversacional configurado a
través de `gateway.py`, igual que el texto de la conversación; no se envía a un
proveedor de búsqueda web.

El cuerpo no se duplica en eventos SSE, `assistant_messages.actions` ni estado
Realtime. Las entradas de mutación se sustituyen por longitud y SHA-256; las
actividades de lectura guardan solo metadatos. Realtime conserva una referencia
a la revisión inmutable y reconstruye la repetición exacta desde PostgreSQL, sin
persistir otra copia del resultado del modelo.

La frontera entre borradores y contenido externo es bidireccional y se aplica
en código:

- después de crear, actualizar o leer un cuerpo, o de leer los extractos del
  historial del lienzo, `web_search` y `read_web_page` quedan bloqueadas durante
  ese turno;
- después de introducir resultados web no confiables, la política post-taint
  bloquea las herramientas del lienzo;
- investigar en web y editar un borrador se realiza en turnos separados;
- la búsqueda en el corpus jurídico interno aprobado sí puede fundamentar la
  redacción.

La vista previa del navegador excluye imágenes Markdown para evitar peticiones
de seguimiento a terceros y abre los enlaces en una pestaña separada con
`noopener`/`noreferrer`.

## Interfaz

El panel contextual junto al chat ofrece selector y creación, editor, vista
previa segura, historial, restauración, recuento, copia y descarga Markdown.
Las acciones del asistente lo abren automáticamente y cada mensaje conserva un
botón para reabrir el borrador referido. Hay autoguardado con debounce,
`Ctrl`/`Cmd`+`S`, guardado antes de enviar o cambiar de conversación y aviso al
abandonar la página. Mientras Anacleto actúa, se guarda o existe un conflicto,
la edición queda bloqueada según corresponda.

## Fuera de alcance de esta versión

- aprobación, firma, publicación o incorporación automática al corpus;
- conversión a `Document`, `Ordinance`, DOCX o PDF;
- compartición o coedición entre usuarios;
- diferencias visuales completas entre revisiones;
- recuperación de archivados desde la interfaz;
- política definitiva de retención y kill switch independiente del lienzo.
