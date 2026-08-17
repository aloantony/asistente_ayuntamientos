from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import get_current_user
from app.core.pagination import PageParams, page_params, paginate
from app.db.session import get_db
from app.projects.models import Project
from app.staff.models import StaffWorker
from app.tasks.access import (
    get_task_organization_for_read,
    get_task_organization_for_write,
    require_task_permission,
)
from app.tasks.models import (
    MUNICIPAL_TASK_OPEN_STATUSES,
    MUNICIPAL_TASK_TERMINAL_STATUSES,
    MunicipalTask,
    MunicipalTaskEvent,
)
from app.tasks.schemas import (
    MunicipalTaskCreate,
    MunicipalTaskDetail,
    MunicipalTaskPriority,
    MunicipalTaskRead,
    MunicipalTaskStatus,
    MunicipalTaskSummary,
    MunicipalTaskTransition,
    MunicipalTaskUpdate,
)
from app.users.models import User

router = APIRouter(prefix="/tasks", tags=["tasks"])

TERMINAL_STATUSES = set(MUNICIPAL_TASK_TERMINAL_STATUSES)

# Una tarea completada o cancelada no se edita: se reabre a `pending` y desde
# ahí vuelve a moverse. Así el histórico refleja la reapertura en lugar de
# disimularla como un salto directo.
TRANSITIONS: dict[str, set[str]] = {
    "pending": {"in_progress", "blocked", "completed", "cancelled"},
    "in_progress": {"pending", "blocked", "completed", "cancelled"},
    "blocked": {"pending", "in_progress", "cancelled"},
    "completed": {"pending"},
    "cancelled": {"pending"},
}

_TRACKED_FIELDS = (
    "title",
    "description",
    "priority",
    "due_date",
    "project_id",
)


def overdue_condition():
    """Vencida = fecha límite pasada y la tarea aún abierta.

    Se evalúa en la consulta contra `current_date` del servidor, nunca contra
    una columna: guardarla obligaría a reescribir filas cada medianoche y a
    convivir con datos que envejecen mal.
    """
    return and_(
        MunicipalTask.due_date.is_not(None),
        MunicipalTask.due_date < func.current_date(),
        MunicipalTask.status.in_(MUNICIPAL_TASK_OPEN_STATUSES),
    )


@router.get("/summary", response_model=MunicipalTaskSummary)
def get_task_summary(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalTaskSummary:
    get_task_organization_for_read(db, organization_id)
    require_task_permission(db, current_user, organization_id, "tasks.view")

    def count_where(*conditions) -> int:
        return (
            db.scalar(
                select(func.count())
                .select_from(MunicipalTask)
                .where(
                    MunicipalTask.organization_id == organization_id,
                    *conditions,
                )
            )
            or 0
        )

    reference_date = db.scalar(select(func.current_date()))
    return MunicipalTaskSummary(
        total=count_where(),
        pending=count_where(MunicipalTask.status == "pending"),
        in_progress=count_where(MunicipalTask.status == "in_progress"),
        blocked=count_where(MunicipalTask.status == "blocked"),
        completed=count_where(MunicipalTask.status == "completed"),
        cancelled=count_where(MunicipalTask.status == "cancelled"),
        overdue=count_where(overdue_condition()),
        unassigned=count_where(
            MunicipalTask.assignee_worker_id.is_(None),
            MunicipalTask.status.in_(MUNICIPAL_TASK_OPEN_STATUSES),
        ),
        reference_date=reference_date,
    )


@router.get("", response_model=list[MunicipalTaskRead])
def list_tasks(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    q: Annotated[str | None, Query(max_length=200)] = None,
    status_filter: Annotated[
        MunicipalTaskStatus | None,
        Query(alias="status"),
    ] = None,
    priority: MunicipalTaskPriority | None = None,
    assignee_worker_id: int | None = None,
    project_id: int | None = None,
    unassigned: bool = False,
    overdue: bool = False,
    include_closed: bool = False,
) -> list[MunicipalTask]:
    get_task_organization_for_read(db, organization_id)
    require_task_permission(db, current_user, organization_id, "tasks.view")

    query = (
        select_tasks()
        .where(MunicipalTask.organization_id == organization_id)
        # Lo urgente y lo que vence antes, primero; sin fecha, al final.
        .order_by(
            MunicipalTask.due_date.is_(None),
            MunicipalTask.due_date,
            MunicipalTask.id,
        )
    )
    if q and q.strip():
        search_text = f"%{q.strip()}%"
        query = query.where(
            or_(
                MunicipalTask.title.ilike(search_text),
                MunicipalTask.description.ilike(search_text),
            )
        )
    if status_filter is not None:
        query = query.where(MunicipalTask.status == status_filter)
    elif not include_closed:
        query = query.where(MunicipalTask.status.in_(MUNICIPAL_TASK_OPEN_STATUSES))
    if priority is not None:
        query = query.where(MunicipalTask.priority == priority)
    if assignee_worker_id is not None:
        query = query.where(
            MunicipalTask.assignee_worker_id == assignee_worker_id
        )
    if unassigned:
        query = query.where(MunicipalTask.assignee_worker_id.is_(None))
    if overdue:
        query = query.where(overdue_condition())
    if project_id is not None:
        query = query.where(MunicipalTask.project_id == project_id)

    return list(db.scalars(paginate(db, query, page, response)))


@router.post(
    "",
    response_model=MunicipalTaskDetail,
    status_code=status.HTTP_201_CREATED,
)
def create_task(
    payload: MunicipalTaskCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalTask:
    get_task_organization_for_write(db, payload.organization_id)
    require_task_permission(
        db,
        current_user,
        payload.organization_id,
        "tasks.create",
    )
    if payload.assignee_worker_id is not None:
        ensure_assignee_is_valid(
            db,
            worker_id=payload.assignee_worker_id,
            organization_id=payload.organization_id,
        )
    if payload.project_id is not None:
        ensure_project_is_valid(
            db,
            project_id=payload.project_id,
            organization_id=payload.organization_id,
        )

    task = MunicipalTask(
        **payload.model_dump(),
        created_by_id=current_user.id,
        updated_by_id=current_user.id,
    )
    db.add(task)
    commit_or_conflict(db, "Task could not be saved")
    db.refresh(task)
    record_event(
        db,
        task=task,
        event_type="created",
        actor_id=current_user.id,
        to_status=task.status,
    )
    return get_existing_task(db, task.id, include_events=True)


@router.get("/{task_id}", response_model=MunicipalTaskDetail)
def get_task(
    task_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalTask:
    task = get_existing_task(db, task_id, include_events=True)
    get_task_organization_for_read(db, task.organization_id)
    require_task_permission(db, current_user, task.organization_id, "tasks.view")
    return task


@router.patch("/{task_id}", response_model=MunicipalTaskDetail)
def update_task(
    task_id: int,
    payload: MunicipalTaskUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalTask:
    task = get_existing_task(db, task_id)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        get_task_organization_for_read(db, task.organization_id)
        require_task_permission(
            db,
            current_user,
            task.organization_id,
            "tasks.view",
        )
        return get_existing_task(db, task_id, include_events=True)

    get_task_organization_for_write(db, task.organization_id)
    require_task_permission(db, current_user, task.organization_id, "tasks.edit")
    if task.status in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Reopen the task before editing it",
        )
    if updates.get("assignee_worker_id") is not None:
        ensure_assignee_is_valid(
            db,
            worker_id=updates["assignee_worker_id"],
            organization_id=task.organization_id,
        )
    if updates.get("project_id") is not None:
        ensure_project_is_valid(
            db,
            project_id=updates["project_id"],
            organization_id=task.organization_id,
        )

    previous_assignee_id = task.assignee_worker_id
    changed_fields = [
        field
        for field in _TRACKED_FIELDS
        if field in updates and updates[field] != getattr(task, field)
    ]

    for field, value in updates.items():
        setattr(task, field, value)
    task.updated_by_id = current_user.id
    commit_or_conflict(db, "Task could not be saved")
    db.refresh(task)

    if task.assignee_worker_id != previous_assignee_id:
        record_event(
            db,
            task=task,
            event_type="assigned",
            actor_id=current_user.id,
        )
    if changed_fields:
        record_event(
            db,
            task=task,
            event_type="updated",
            actor_id=current_user.id,
            changed_fields=changed_fields,
        )

    return get_existing_task(db, task.id, include_events=True)


@router.post("/{task_id}/transition", response_model=MunicipalTaskDetail)
def transition_task(
    task_id: int,
    payload: MunicipalTaskTransition,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalTask:
    existing = get_existing_task(db, task_id)
    is_reopening = (
        existing.status in TERMINAL_STATUSES and payload.status == "pending"
    )
    needs_manager = payload.status == "cancelled" or is_reopening
    require_task_permission(
        db,
        current_user,
        existing.organization_id,
        "tasks.manage" if needs_manager else "tasks.edit",
    )
    get_task_organization_for_write(db, existing.organization_id)

    task = get_locked_task(db, task_id)
    target_status = payload.status
    if target_status == task.status or target_status not in TRANSITIONS[task.status]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Invalid task transition: {task.status} -> {target_status}",
        )
    # Bloquear sin decir qué lo bloquea deja una tarea que nadie sabe desatascar;
    # cancelar o reabrir sin explicación borra el porqué de la decisión.
    if target_status in {"blocked", "cancelled"} or is_reopening:
        if payload.reason is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A reason is required to block, cancel or reopen a task",
            )

    previous_status = task.status
    task.status = target_status
    task.blocked_reason = payload.reason if target_status == "blocked" else None
    task.completed_at = (
        func.now() if target_status == "completed" else None
    )
    task.updated_by_id = current_user.id
    commit_or_conflict(db, "Task could not be saved")
    db.refresh(task)

    record_event(
        db,
        task=task,
        event_type="status_changed",
        actor_id=current_user.id,
        from_status=previous_status,
        to_status=target_status,
        changed_fields=["status"],
        note=payload.reason,
    )
    return get_existing_task(db, task.id, include_events=True)


def select_tasks(*, include_events: bool = False) -> Select:
    options = [
        selectinload(MunicipalTask.assignee),
        selectinload(MunicipalTask.project),
    ]
    # El histórico solo se carga en el detalle: la lista no paga su coste.
    if include_events:
        options.append(selectinload(MunicipalTask.events))
    return (
        select(MunicipalTask)
        .options(*options)
        .execution_options(populate_existing=True)
    )


def get_existing_task(
    db: Session,
    task_id: int,
    *,
    include_events: bool = False,
) -> MunicipalTask:
    task = db.scalar(
        select_tasks(include_events=include_events).where(
            MunicipalTask.id == task_id
        )
    )
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )
    return task


def get_locked_task(db: Session, task_id: int) -> MunicipalTask:
    task = db.scalar(
        select(MunicipalTask)
        .where(MunicipalTask.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )
    return task


def ensure_assignee_is_valid(
    db: Session,
    *,
    worker_id: int,
    organization_id: int,
) -> StaffWorker:
    worker = db.get(StaffWorker, worker_id)
    if worker is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Staff worker not found",
        )
    if worker.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Staff worker does not belong to the organization",
        )
    if worker.status == "archived":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Archived staff workers cannot take tasks",
        )
    return worker


def ensure_project_is_valid(
    db: Session,
    *,
    project_id: int,
    organization_id: int,
) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )
    if project.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Project does not belong to the organization",
        )
    if project.status == "archived":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Archived projects cannot take tasks",
        )
    return project


def record_event(
    db: Session,
    *,
    task: MunicipalTask,
    event_type: str,
    actor_id: int | None,
    from_status: str | None = None,
    to_status: str | None = None,
    changed_fields: list[str] | None = None,
    note: str | None = None,
) -> None:
    event = MunicipalTaskEvent(
        task_id=task.id,
        organization_id=task.organization_id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        changed_fields=changed_fields or [],
        note=note,
        actor_id=actor_id,
    )
    db.add(event)
    db.commit()


def commit_or_conflict(db: Session, detail: str) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
        ) from None
