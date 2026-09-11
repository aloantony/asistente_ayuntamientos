from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MunicipalTaskStatus = Literal[
    "pending",
    "in_progress",
    "blocked",
    "completed",
    "cancelled",
]
MunicipalTaskPriority = Literal["low", "normal", "high", "urgent"]
MunicipalTaskEventType = Literal[
    "created",
    "updated",
    "status_changed",
    "assigned",
]


def blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


class TaskAssigneeRead(BaseModel):
    """Lo justo del trabajador para pintar la tarea sin abrir su ficha."""

    id: int
    full_name: str

    model_config = ConfigDict(from_attributes=True)


class TaskProjectRead(BaseModel):
    id: int
    name: str

    model_config = ConfigDict(from_attributes=True)


class MunicipalTaskRead(BaseModel):
    id: int
    organization_id: int
    title: str
    description: str | None
    status: MunicipalTaskStatus
    priority: MunicipalTaskPriority
    due_date: date | None
    blocked_reason: str | None
    completed_at: datetime | None
    assignee_worker_id: int | None
    project_id: int | None
    created_by_id: int | None
    updated_by_id: int | None
    created_at: datetime
    updated_at: datetime
    assignee: TaskAssigneeRead | None
    project: TaskProjectRead | None

    model_config = ConfigDict(from_attributes=True)


class MunicipalTaskCreate(BaseModel):
    organization_id: int
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    priority: MunicipalTaskPriority = "normal"
    due_date: date | None = None
    assignee_worker_id: int | None = None
    project_id: int | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("description", mode="before")(
        blank_to_none
    )


class MunicipalTaskUpdate(BaseModel):
    """Campos de la ficha. El estado se mueve por `/transition`, no por aquí.

    Separar la edición del cambio de estado evita que un parche cualquiera
    arrastre una transición sin pasar por sus reglas ni dejar evento.
    """

    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    priority: MunicipalTaskPriority | None = None
    due_date: date | None = None
    assignee_worker_id: int | None = None
    project_id: int | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("description", mode="before")(
        blank_to_none
    )

    @model_validator(mode="after")
    def reject_null_required_values(self):
        for field_name in ("title", "priority"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class MunicipalTaskTransition(BaseModel):
    status: MunicipalTaskStatus
    # Bloquear exige motivo; cancelar y reabrir, explicación. La ruta comprueba
    # cuál de las dos aplica.
    reason: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("reason", mode="before")(blank_to_none)


class MunicipalTaskEventRead(BaseModel):
    id: int
    task_id: int
    organization_id: int
    event_type: MunicipalTaskEventType
    from_status: MunicipalTaskStatus | None
    to_status: MunicipalTaskStatus | None
    changed_fields: list[str]
    note: str | None
    actor_id: int | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MunicipalTaskDetail(MunicipalTaskRead):
    events: list[MunicipalTaskEventRead]


class MunicipalTaskSummary(BaseModel):
    """Conteos de la banda de filtros de la hoja de ruta.

    `overdue` se calcula contra la fecha del servidor en cada consulta: una
    tarea vencida no es un estado guardado, sino una lectura del calendario.
    """

    total: int
    pending: int
    in_progress: int
    blocked: int
    completed: int
    cancelled: int
    overdue: int
    unassigned: int
    reference_date: date


class TaskLinkOptionRead(BaseModel):
    id: int
    name: str
