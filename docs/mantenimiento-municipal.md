# Mantenimiento municipal

Actualizado: 2026-07-15.

## Propósito

El dominio `maintenance` programa y audita intervenciones sobre activos del
inventario municipal. Es una capa operativa separada tanto del inventario
(`assets`) como de las tareas internas de la oficina de agentes
(`agent_office`): una orden de mantenimiento representa trabajo humano sobre
un activo real, no una ejecución del asistente.

La primera versión cubre un flujo vertical y controlado:

- consultar las órdenes visibles de una organización o un activo;
- programar una orden preventiva, correctiva, de inspección, limpieza u otra;
- editar sus datos operativos sin alterar directamente el estado;
- iniciar, completar, cancelar o reabrir la orden mediante una máquina de
  estados explícita;
- conservar un historial inmutable de creación, edición y transiciones.

## Modelo y aislamiento tenant

`maintenance_orders` guarda la orden, su activo, organización y municipio,
tipo, prioridad, estado, fecha prevista, tiempo estimado y asignación opcional.
La organización y el municipio se derivan siempre del activo; el cliente no
puede elegirlos ni mover una orden entre tenants.

La relación con `municipal_assets` se protege mediante una clave foránea
compuesta `(asset_id, organization_id, municipality_id)`. Las referencias a
usuarios se aceptan solo cuando el usuario está activo y pertenece a la misma
organización. Los índices siguen los accesos principales: organización más
estado/fecha, activo más estado/fecha y asignación más estado/fecha.

`maintenance_order_events` es un registro append-only. Cada evento conserva
la organización, la orden, el tipo de cambio, la transición de estado cuando
existe, la lista blanca de campos modificados, una nota acotada y el actor. No
hay endpoints de actualización o borrado de eventos, ni borrado físico de
órdenes. Sus claves foráneas usan `RESTRICT` y la migración instala una guarda
en PostgreSQL que rechaza `UPDATE` y `DELETE` sobre el historial.

## Estados y concurrencia

Los estados son:

- `planned`;
- `scheduled`;
- `in_progress`;
- `completed`;
- `cancelled`.

La API no permite cambiar `status` mediante el `PATCH` de metadatos. Una orden
nace `planned` sin fecha o `scheduled` cuando recibe `scheduled_for`. El grafo
permitido es:

- `planned → scheduled|in_progress` con edición ordinaria;
- `scheduled → planned|in_progress` con edición ordinaria;
- `in_progress → completed` con permiso de finalización;
- cualquier estado abierto → `cancelled`, solo para `maintenance.manage` y
  con motivo;
- `completed|cancelled → planned`, solo para `maintenance.manage` y con
  motivo.

Pasar a `scheduled` exige fecha y pasar a `planned` la limpia. No se puede
completar una orden sin iniciarla antes. Completar exige
`maintenance.complete|manage`; programar, desprogramar e iniciar exigen
`maintenance.edit|manage`; cancelar, reabrir y editar metadatos de una orden
terminal quedan reservados a `maintenance.manage`. Cancelar o reabrir exige
siempre una nota no vacía y acotada.

Toda transición bloquea primero el activo y después la orden, valida de nuevo
la asignación y escribe el evento en la misma transacción. Dos transiciones
concurrentes desde el mismo estado producen un único éxito; la segunda observa
el estado nuevo y responde conflicto sin duplicar el evento.

Las transacciones no contienen llamadas externas ni trabajo de IA. La futura
automatización por Anacleto deberá invocar el servicio de mantenimiento con un
actor autorizado y conservar exactamente las mismas reglas y auditoría.

## API

La primera versión expone:

- `GET /maintenance/orders`;
- `POST /maintenance/orders`;
- `GET /maintenance/orders/{order_id}`;
- `PATCH /maintenance/orders/{order_id}`;
- `POST /maintenance/orders/{order_id}/transition`.

El listado se filtra en SQL antes de paginar y permite acotar por organización,
activo, estado, fecha y asignación. El detalle carga sus eventos en bloque. Los
recursos invisibles o ajenos responden con un 404 genérico antes de revelar si
el identificador existe. No se acepta JSON arbitrario para el historial y los
esquemas de escritura rechazan campos desconocidos.

## Permisos

Mantenimiento usa permisos propios, evaluados dentro de la organización:

- `maintenance.view`;
- `maintenance.create`;
- `maintenance.edit`;
- `maintenance.complete`;
- `maintenance.manage`.

`maintenance.manage` incluye las acciones anteriores, pero no sustituye la
visibilidad del activo. Cada lectura o escritura exige simultáneamente el
permiso `maintenance.*|manage` apropiado y `assets.view|manage` en la
organización canónica de la orden, de modo que el dominio operativo no
convierta en visible un activo inaccesible. Crear o modificar exige organización
y municipio activos; una organización pausada conserva solo lectura y una
archivada no expone órdenes.

Crear rechaza activos retirados o archivados. Al retirar o archivar un activo,
el inventario bloquea primero el activo y comprueba que no tenga órdenes
abiertas; si las hay responde conflicto para que el usuario las cierre o
cancele. Así crear una orden y retirar su activo no puede ganar una carrera en
direcciones distintas.

## Interfaz

El detalle de un marcador de activo en `/mapa` muestra su mantenimiento sin
crear una segunda fuente de verdad del inventario. El panel separa próximas
órdenes e historial, destaca vencimientos, permite programar una intervención
y ofrece solo las transiciones autorizadas. Los errores 403 del backend siguen
siendo la garantía final cuando la sesión todavía no puede expresar permisos
por organización con precisión.

## Minimización, auditoría y retención

Los títulos, descripciones y notas son datos operativos; no deben contener
información personal innecesaria, datos de salud ni credenciales. La asignación
guarda un identificador interno de usuario y no una copia de sus datos. Se
valida que el usuario esté activo y sea miembro de la organización al asignar y
de nuevo antes de iniciar o completar el trabajo. Los eventos registran una
lista tipada de campos modificados y las transiciones, evitando duplicar en un
payload valores libres que no sean necesarios para la trazabilidad.

La política temporal es conservación mientras la orden forme parte del
historial operativo del activo. Antes de producción con datos reales debe
acordarse el plazo institucional y un proceso de exportación/archivo; no se
habilita borrado destructivo desde la API.

## Fuera de alcance

Esta entrega no incorpora todavía:

- recurrencias automáticas ni creación de la siguiente orden;
- notificaciones externas, cron o colas;
- partes de horas, costes, facturación o productividad de personal;
- fotos, adjuntos o importaciones masivas;
- configuración libre de tipos, estados, prioridades o campos;
- edición completa del inventario fuera del mapa;
- ejecución autónoma por el asistente.

## Validación esperada

```bash
python3 -m compileall -q backend/app backend/alembic backend/tests
```

```bash
python -m pytest tests/test_maintenance.py tests/test_assets.py tests/test_migrations.py -q
```

La aceptación final incluye además la suite backend completa, `npm run
typecheck`, `npm run lint`, `npm run build` y una validación de navegador del
flujo de programación y transición desde `/mapa`.
