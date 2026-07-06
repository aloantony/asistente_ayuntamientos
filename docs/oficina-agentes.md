# Oficina de agentes municipales

Actualizado: 2026-06-29.

## Alcance

La oficina de agentes v1 añade una capa interna de tareas delegables para Anacleto. Obsidian queda fuera del diseño.

El objetivo es una cola auditable de trabajo: cada acción usa permisos del usuario, queda registrada y puede exigir aprobación humana.

Anacleto sigue siendo el único interlocutor visible. Los departamentos de la oficina son capacidades internas para enrutar, aprobar y ejecutar trabajo; no deben presentarse como voces separadas ante el usuario.

## Capacidades internas v1

| Departamento | Capacidad interna | Alcance |
| --- | --- | --- |
| `front_desk` | Anacleto Recepción | Recibir peticiones, convertirlas en tareas y derivarlas. |
| `requirements` | Necesidades | Consultar, crear y actualizar necesidades como borradores supervisados. |
| `ordinances` | Ordenanzas | Buscar normativa ya importada, aprobada y vectorizada. |
| `documents` | Documental | Preparar planes de trabajo documental; lectura automática de documentos queda fuera de v1. |
| `projects` | Proyectos | Consultar proyectos visibles. |
| `map` | Mapa | Consultar ubicaciones visibles de proyectos y necesidades. |
| `daily_briefing` | Informe diario | Preparar resúmenes diarios de proyectos y necesidades visibles. |

## Permisos

- `agent_office.view`: ver tareas y rutinas.
- `agent_office.create`: crear tareas delegadas.
- `agent_office.approve`: aprobar o cancelar tareas.
- `agent_office.execute`: ejecutar tareas aprobadas.
- `agent_office.manage`: gestionar la oficina y rutinas.

Las tareas se delimitan por `organization_id`. Aprobar y ejecutar exige permiso explícito en esa organización.

## Flujo

1. Se crea una tarea con organización, título, descripción, departamento opcional y acción solicitada.
2. Si no hay departamento explícito, el backend solo infiere por `requested_action`; si tampoco hay acción estructurada, entra por `front_desk`/`triage`. No se enruta semánticamente por palabras sueltas del texto libre.
3. Las acciones que modifican datos fuerzan aprobación humana aunque el cliente pida `approval_policy=never`.
4. Las tareas aprobadas se ejecutan con el usuario solicitante como contexto RBAC.
5. Cada cambio de estado crea un evento auditable.

Estados: `pending_approval`, `approved`, `queued`, `running`, `waiting_approval`, `completed`, `failed`, `cancelled`.

## Endpoints

- `GET /agent-office/status`
- `GET /agent-office/tasks`
- `POST /agent-office/tasks`
- `GET /agent-office/tasks/{task_id}`
- `PATCH /agent-office/tasks/{task_id}/approval`
- `POST /agent-office/tasks/{task_id}/enqueue`
- `POST /agent-office/tasks/{task_id}/run-inline`
- `GET /agent-office/routines`
- `POST /agent-office/routines`
- `POST /agent-office/routines/{routine_id}/trigger`

## Rutinas

La rutina v1 es `daily_briefing`. Puede crear una tarea aprobada de informe diario. La planificación horaria automática queda para despliegue; en desarrollo se dispara manualmente con `POST /agent-office/routines/{routine_id}/trigger`.

## Restricciones

- Sin Obsidian.
- Hermes Agent o el modelo no deciden permisos.
- Las herramientas reales siguen en el backend propio.
- El texto libre no debe activar rutas de producto por marcadores como “ordenanza”, “mapa” o “necesidad”; esa decisión pertenece al planner semántico de Anacleto o a una acción estructurada.
- El usuario solicitante es el contexto de ejecución RBAC.
- Las acciones que modifican datos requieren aprobación humana por defecto.
- Los documentos originales siguen fuera de llamadas a IA.
