from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from sqlalchemy.orm import Session

from app.agent_office.schemas import (
    AgentOfficeRoutineCreate,
    AgentOfficeRoutineRead,
    AgentOfficeStatusRead,
    AgentOfficeTaskApproval,
    AgentOfficeTaskCreate,
    AgentOfficeTaskDetail,
    AgentOfficeTaskEnqueueRead,
    AgentOfficeTaskRead,
)
from app.agent_office.service import (
    OFFICE_AGENTS,
    approve_or_cancel_task,
    create_routine,
    create_task,
    get_routine_for_user,
    get_task_for_user,
    list_routines_for_user,
    list_tasks_for_user,
    mark_task_queued,
    run_agent_office_task,
    trigger_routine,
)
from app.auth.dependencies import get_current_user
from app.core.jobs import get_default_queue
from app.db.session import get_db
from app.users.models import User

router = APIRouter(prefix="/agent-office", tags=["agent-office"])


@router.get("/status", response_model=AgentOfficeStatusRead)
def get_agent_office_status(
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    return {
        "enabled": True,
        "agents": [agent.metadata for agent in OFFICE_AGENTS.values()],
        "default_approval_policy": "before_execution",
        "scheduler": {
            "supported": True,
            "mode": "rq/manual-trigger-v1",
            "notes": "Routines can create supervised tasks; automatic cron wiring is deployment-level.",
        },
    }


@router.get("/tasks", response_model=list[AgentOfficeTaskRead])
def list_agent_office_tasks(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    organization_id: int | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    department: str | None = None,
):
    return list_tasks_for_user(
        db,
        current_user,
        organization_id=organization_id,
        status_filter=status_filter,
        department=department,
    )


@router.post("/tasks", response_model=AgentOfficeTaskDetail, status_code=http_status.HTTP_201_CREATED)
def create_agent_office_task(
    payload: AgentOfficeTaskCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    return create_task(
        db,
        current_user,
        organization_id=payload.organization_id,
        title=payload.title,
        description=payload.description,
        department=payload.department,
        requested_action=payload.requested_action,
        priority=payload.priority,
        approval_policy=payload.approval_policy,
        requires_human_approval=payload.requires_human_approval,
        input_payload=payload.input,
        due_at=payload.due_at,
        scheduled_for=payload.scheduled_for,
    )


@router.get("/tasks/{task_id}", response_model=AgentOfficeTaskDetail)
def get_agent_office_task(
    task_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    return get_task_for_user(db, current_user, task_id)


@router.patch("/tasks/{task_id}/approval", response_model=AgentOfficeTaskDetail)
def approve_agent_office_task(
    task_id: int,
    payload: AgentOfficeTaskApproval,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    task = get_task_for_user(db, current_user, task_id)
    return approve_or_cancel_task(
        db,
        current_user,
        task,
        decision=payload.decision,
        notes=payload.notes,
    )


@router.post("/tasks/{task_id}/enqueue", response_model=AgentOfficeTaskEnqueueRead)
def enqueue_agent_office_task(
    task_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AgentOfficeTaskEnqueueRead:
    task = get_task_for_user(db, current_user, task_id)
    task = mark_task_queued(db, current_user, task)
    try:
        queue_job = get_default_queue().enqueue(run_agent_office_task, task.id)
    except Exception as error:
        task.status = "failed"
        task.error_message = str(error)[:2000]
        db.commit()
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent office queue is unavailable",
        ) from error
    return AgentOfficeTaskEnqueueRead(
        task_id=task.id,
        status="queued",
        queue_job_id=queue_job.id,
    )


@router.post("/tasks/{task_id}/run-inline", response_model=AgentOfficeTaskDetail)
def run_agent_office_task_inline(
    task_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    task = get_task_for_user(db, current_user, task_id)
    task = mark_task_queued(db, current_user, task)
    run_agent_office_task(task.id, db=db)
    return get_task_for_user(db, current_user, task_id)


@router.get("/routines", response_model=list[AgentOfficeRoutineRead])
def list_agent_office_routines(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    organization_id: int | None = None,
):
    return list_routines_for_user(db, current_user, organization_id=organization_id)


@router.post("/routines", response_model=AgentOfficeRoutineRead, status_code=http_status.HTTP_201_CREATED)
def create_agent_office_routine(
    payload: AgentOfficeRoutineCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    return create_routine(
        db,
        current_user,
        organization_id=payload.organization_id,
        name=payload.name,
        kind=payload.kind,
        routine_status=payload.status,
        cadence=payload.cadence,
        schedule_time=payload.schedule_time,
        timezone_name=payload.timezone,
        target_channel=payload.target_channel,
        config=payload.config,
        next_run_at=payload.next_run_at,
    )


@router.post("/routines/{routine_id}/trigger", response_model=AgentOfficeTaskDetail)
def trigger_agent_office_routine(
    routine_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    routine = get_routine_for_user(db, current_user, routine_id)
    task = trigger_routine(db, current_user, routine)
    return get_task_for_user(db, current_user, task.id)
