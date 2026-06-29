# Oficina de agentes municipales

Actualizado: 2026-06-29.

## Alcance

La oficina de agentes v1 añade una capa de tareas delegables a agentes municipales especialistas. Obsidian queda fuera del diseño.

El objetivo es una cola auditable de trabajo: cada acción usa permisos del usuario, queda registrada y puede exigir aprobación humana.

## Agentes v1

| Departamento | Agente | Alcance |
| --- | --- | --- |
| `front_desk` | Anacleto Recepción | Recibir peticiones, convertirlas en tareas y derivarlas. |
| `requirements` | Agente de necesidades | Consultar, crear y actualizar necesidades como borradores supervisados. |
| `ordinances` | Agente de ordenanzas | Buscar normativa ya importada, aprobada y vectorizada. |
| `documents` | Agente documental | Preparar planes de trabajo documental; lectura automática de documentos queda fuera de v1. |
| `projects` | Agente de proyectos | Consultar proyectos visibles. |
| `map` | Agente de mapa | Consultar ubicaciones visibles de proyectos y necesidades. |
| `admin_feedback` | Agente de feedback | Registrar fricciones, bugs y mejoras para administración. |
| `daily_briefing` | Agente de informe diario | Preparar resúmenes diarios de proyectos y necesidades visibles. |

## Permisos

- `agent_office.view`: ver tareas y rutinas.
- `agent_office.create`: crear tareas delegadas.
- `agent_office.approve`: aprobar o cancelar tareas.
- `agent_office.execute`: ejecutar tareas aprobadas.
- `agent_office.manage`: gestionar la oficina y rutinas.

Las tareas se delimitan por `organization_id`. Aprobar y ejecutar exige permiso explícito en esa organización.

## Flujo

1. Se crea una tarea con organización, título, descripción, departamento opcional y acción solicitada.
2. El backend infiere el departamento si no viene especificado.
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
- El usuario solicitante es el contexto de ejecución RBAC.
- Las acciones que modifican datos requieren aprobación humana por defecto.
- Los documentos originales siguen fuera de llamadas a IA.
